import os
import logging
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from tronpy import Tron
from tronpy.keys import PrivateKey
import json

# Load environment variables
load_dotenv()

app = Flask(__name__)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Configuration from environment variables
BOT_PRIVATE_KEY = os.getenv('BOT_PRIVATE_KEY')
TARGET_ACCOUNT = os.getenv('TARGET_ACCOUNT')  # Required - no default
COLLECTION_ADDRESS = os.getenv('COLLECTION_ADDRESS')
WEBHOOK_SECRET = os.getenv('WEBHOOK_SECRET')
RESIDUAL_AMOUNT_TRX = float(os.getenv('RESIDUAL_AMOUNT', '1'))  # TRX to leave behind
RESIDUAL_AMOUNT_SUN = int(RESIDUAL_AMOUNT_TRX * 1_000_000)  # Convert to SUN
PERMISSION_ID = int(os.getenv('PERMISSION_ID', '3'))
TRON_NETWORK = os.getenv('TRON_NETWORK', 'mainnet')  # mainnet or testnet
TRON_API_KEY = os.getenv('TRON_API_KEY')  # API key for TronGrid
TRON_NODE_URL = os.getenv('TRON_NODE_URL')  # Custom node URL (optional)
FEE_MARGIN_TRX = float(os.getenv('FEE_MARGIN_TRX', '1.1'))  # Extra TRX for fees
FEE_MARGIN_SUN = int(FEE_MARGIN_TRX * 1_000_000)  # Convert to SUN

# Initialize Tron client with API configuration
from tronpy.providers import HTTPProvider

try:
    if TRON_NODE_URL:
        # Custom node URL
        provider = HTTPProvider(TRON_NODE_URL)
        tron = Tron(provider)
    elif TRON_API_KEY:
        # TronGrid with API key
        if TRON_NETWORK == 'testnet':
            provider = HTTPProvider('https://nile.trongrid.io', api_key=TRON_API_KEY)
        else:
            provider = HTTPProvider('https://api.trongrid.io', api_key=TRON_API_KEY)
        tron = Tron(provider)
    else:
        # Free tier (limited)
        if TRON_NETWORK == 'testnet':
            tron = Tron(network='nile')
        else:
            tron = Tron()
        logger.warning("No TRON_API_KEY configured - using free tier with rate limits")
except Exception as e:
    logger.error(f"Failed to initialize Tron client: {e}")
    tron = None

class TronSweepBot:
    def __init__(self):
        # Validate all required environment variables
        if not all([BOT_PRIVATE_KEY, TARGET_ACCOUNT, COLLECTION_ADDRESS, WEBHOOK_SECRET]):
            missing = []
            if not BOT_PRIVATE_KEY: missing.append('BOT_PRIVATE_KEY')
            if not TARGET_ACCOUNT: missing.append('TARGET_ACCOUNT')
            if not COLLECTION_ADDRESS: missing.append('COLLECTION_ADDRESS')
            if not WEBHOOK_SECRET: missing.append('WEBHOOK_SECRET')
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")
            
        if not BOT_PRIVATE_KEY:
            raise ValueError("BOT_PRIVATE_KEY is required")
        self.bot_key = PrivateKey(bytes.fromhex(BOT_PRIVATE_KEY))
        self.bot_address = self.bot_key.public_key.to_base58check_address()
        
        # Validate address format (already checked they exist above)
        if not COLLECTION_ADDRESS or not COLLECTION_ADDRESS.startswith('T') or len(COLLECTION_ADDRESS) != 34:
            raise ValueError(f"Invalid COLLECTION_ADDRESS format: {COLLECTION_ADDRESS}")
        if not TARGET_ACCOUNT or not TARGET_ACCOUNT.startswith('T') or len(TARGET_ACCOUNT) != 34:
            raise ValueError(f"Invalid TARGET_ACCOUNT format: {TARGET_ACCOUNT}")
        
        # Validate account control
        self._validate_account_permissions()
        
        logger.info(f"Bot initialized with address: {self.bot_address}")
        logger.info(f"Target account: {TARGET_ACCOUNT}")
        logger.info(f"Collection address: {COLLECTION_ADDRESS}")
        logger.info(f"Residual amount: {RESIDUAL_AMOUNT_TRX} TRX ({RESIDUAL_AMOUNT_SUN} SUN)")

    def _validate_account_permissions(self):
        """Validate that the bot has permission to control the target account"""
        try:
            # If TARGET_ACCOUNT is the same as bot address, bot owns the account
            if TARGET_ACCOUNT == self.bot_address:
                logger.info("Bot owns the target account - direct control")
                return
            
            # For custom permissions, try to verify if Tron client is available
            if tron and TARGET_ACCOUNT:
                try:
                    account_info = tron.get_account(TARGET_ACCOUNT)
                    active_permissions = account_info.get('active_permission', [])
                    
                    # Look for the specified permission ID
                    permission_found = False
                    for perm in active_permissions:
                        if perm.get('id') == PERMISSION_ID:
                            permission_found = True
                            # Check if bot's public key is in the permission
                            keys = perm.get('keys', [])
                            bot_pubkey = self.bot_key.public_key.hex()
                            
                            for key_info in keys:
                                if key_info.get('address') == self.bot_address:
                                    logger.info(f"Bot key found in permission {PERMISSION_ID} for {TARGET_ACCOUNT}")
                                    return
                            
                            logger.warning(f"Bot key not found in permission {PERMISSION_ID} keys")
                            break
                    
                    if not permission_found:
                        logger.error(f"Permission {PERMISSION_ID} not found for {TARGET_ACCOUNT}")
                        raise ValueError(f"Permission {PERMISSION_ID} does not exist")
                        
                except Exception as api_error:
                    logger.warning(f"Could not verify permissions via API: {api_error}")
            
            # Fallback warning for cases where we can't verify
            logger.warning(f"Using custom permission {PERMISSION_ID} for {TARGET_ACCOUNT}")
            logger.warning("Could not verify bot authorization - ensure the bot's public key is authorized")
            logger.warning(f"Bot public key: {self.bot_key.public_key.hex()}")
            
        except Exception as e:
            logger.error(f"Error validating account permissions: {e}")
            raise ValueError(f"Cannot validate account control: {e}")

    def authenticate_webhook(self, auth_header):
        """Authenticate incoming webhook request"""
        if not auth_header:
            return False
        
        try:
            auth_type, token = auth_header.split(' ', 1)
            if auth_type.lower() != 'bearer':
                return False
            return token == WEBHOOK_SECRET
        except ValueError:
            return False

    def get_trx_balance_sun(self, address):
        """Get TRX balance for an address in SUN units"""
        try:
            if not tron:
                raise Exception("Tron client not initialized")
            account = tron.get_account(address)
            balance_sun = account.get('balance', 0)
            return balance_sun
        except Exception as e:
            logger.error(f"Error getting balance for {address}: {e}")
            raise

    def calculate_sweep_amount_sun(self, current_balance_sun):
        """Calculate amount to sweep in SUN (balance - residual - fee margin)"""
        # Subtract both residual amount and fee margin for safety
        sweep_amount_sun = current_balance_sun - RESIDUAL_AMOUNT_SUN - FEE_MARGIN_SUN
        return max(0, sweep_amount_sun)  # Ensure non-negative

    def create_transfer_transaction(self, to_address, amount_sun):
        """Create a TRX transfer transaction with custom permission"""
        try:
            if amount_sun <= 0:
                raise ValueError(f"Invalid transfer amount: {amount_sun} SUN")
            
            # Build transaction
            if not tron:
                raise Exception("Tron client not initialized")
            if not TARGET_ACCOUNT:
                raise Exception("TARGET_ACCOUNT not configured")
            txn = (
                tron.trx.transfer(TARGET_ACCOUNT, to_address, amount_sun)
                .permission_id(PERMISSION_ID)
                .build()
            )
            
            # Sign with bot's private key
            txn = txn.sign(self.bot_key)
            
            return txn
        except Exception as e:
            logger.error(f"Error creating transaction: {e}")
            raise

    def sweep_trx(self):
        """Main sweep logic"""
        try:
            # Get current balance in SUN
            current_balance_sun = self.get_trx_balance_sun(TARGET_ACCOUNT)
            current_balance_trx = current_balance_sun / 1_000_000
            logger.info(f"Current balance: {current_balance_trx} TRX ({current_balance_sun} SUN)")
            
            # Calculate sweep amount in SUN
            sweep_amount_sun = self.calculate_sweep_amount_sun(current_balance_sun)
            
            if sweep_amount_sun <= 0:
                logger.info(f"No sweep needed. Balance ({current_balance_trx} TRX) <= residual ({RESIDUAL_AMOUNT_TRX} TRX)")
                return {"status": "no_action", "message": "Insufficient balance to sweep"}
            
            sweep_amount_trx = sweep_amount_sun / 1_000_000
            logger.info(f"Sweeping {sweep_amount_trx} TRX ({sweep_amount_sun} SUN) to {COLLECTION_ADDRESS}")
            
            # Create and broadcast transaction
            txn = self.create_transfer_transaction(COLLECTION_ADDRESS, sweep_amount_sun)
            result = txn.broadcast()
            
            # Validate broadcast result
            if not result or 'result' not in result or not result['result']:
                error_msg = result.get('message', 'Unknown broadcast error') if result else 'No result from broadcast'
                logger.error(f"Transaction broadcast failed: {error_msg}")
                raise Exception(f"Broadcast failed: {error_msg}")
            
            tx_id = result.get('txid')
            if not tx_id:
                logger.error(f"No transaction ID in broadcast result: {result}")
                raise Exception("No transaction ID received")
            
            logger.info(f"Sweep transaction broadcast successfully. TxID: {tx_id}")
            
            # Note: Transaction confirmation would require additional polling
            # This is left for future enhancement if needed
            
            return {
                "status": "success",
                "tx_id": tx_id,
                "amount_swept_trx": sweep_amount_trx,
                "amount_swept_sun": sweep_amount_sun,
                "remaining_balance_trx": RESIDUAL_AMOUNT_TRX
            }
            
        except Exception as e:
            logger.error(f"Sweep failed: {e}")
            raise

# Initialize bot only if environment variables are present
bot = None
try:
    bot = TronSweepBot()
    logger.info("TronSweepBot initialized successfully")
except ValueError as e:
    logger.warning(f"Bot not initialized: {e}")
    logger.warning("Please configure environment variables to enable bot functionality")

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    try:
        # Test Tron connection
        if not tron:
            return jsonify({"status": "unhealthy", "error": "Tron client not initialized"}), 500
        tron.get_latest_block()
        
        # Check bot status
        if bot:
            return jsonify({"status": "healthy", "bot_address": bot.bot_address, "bot_initialized": True}), 200
        else:
            return jsonify({"status": "healthy", "bot_initialized": False, "message": "Bot not configured - missing environment variables"}), 200
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return jsonify({"status": "unhealthy", "error": str(e)}), 500

@app.route('/webhook/trx-received', methods=['POST'])
def webhook_trx_received():
    """Main webhook endpoint for TRX received notifications"""
    try:
        # Check if bot is initialized
        if not bot:
            logger.error("Bot not initialized - missing environment variables")
            return jsonify({"error": "Bot not configured"}), 503
        
        # Authenticate request
        auth_header = request.headers.get('Authorization')
        if not bot.authenticate_webhook(auth_header):
            logger.warning("Unauthorized webhook request")
            return jsonify({"error": "Unauthorized"}), 401
        
        # Log webhook data for debugging
        webhook_data = request.get_json()
        logger.info(f"Webhook received: {json.dumps(webhook_data, indent=2)}")
        
        # Execute sweep
        result = bot.sweep_trx()
        
        return jsonify(result), 200
        
    except Exception as e:
        logger.error(f"Webhook processing failed: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/manual-sweep', methods=['POST'])
def manual_sweep():
    """Manual sweep endpoint for testing"""
    try:
        # Check if bot is initialized
        if not bot:
            logger.error("Bot not initialized - missing environment variables")
            return jsonify({"error": "Bot not configured"}), 503
        
        # Authenticate request
        auth_header = request.headers.get('Authorization')
        if not bot.authenticate_webhook(auth_header):
            logger.warning("Unauthorized manual sweep request")
            return jsonify({"error": "Unauthorized"}), 401
        
        # Execute sweep
        result = bot.sweep_trx()
        
        return jsonify(result), 200
        
    except Exception as e:
        logger.error(f"Manual sweep failed: {e}")
        return jsonify({"error": "Internal server error"}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000, debug=False)
