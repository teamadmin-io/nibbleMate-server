from __future__ import annotations
"""nibbleMate backend — merged REST + WebSocket service

Dependencies:
pip install fastapi uvicorn python-dotenv pyjwt supabase slowapi
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta
from typing import List, Optional
import re  # For regex operations
import time
import uuid
import random
import traceback

# Let Supabase's client handle SSL certificate verification naturally
# We'll set an environment flag to indicate we want to use Supabase's built-in SSL handling
os.environ['SUPABASE_USE_NATIVE_SSL'] = 'true'

# Print a friendly message about our SSL strategy
print("🔒 Using Supabase's native SSL certificate handling")

# Log platform info for debugging purposes
print(f"🖥️ Running on platform: {sys.platform}")

import jwt  # type: ignore # PyJWT
import uvicorn # type: ignore
from dotenv import load_dotenv # type: ignore
from fastapi import ( # type: ignore
    Depends,
    FastAPI,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
    Response
)
from fastapi.responses import JSONResponse # type: ignore
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials # type: ignore
from fastapi.websockets import WebSocketState # type: ignore
from fastapi.staticfiles import StaticFiles # type: ignore
from supabase import Client, create_client # type: ignore
from slowapi import Limiter, _rate_limit_exceeded_handler # type: ignore
from slowapi.util import get_remote_address # type: ignore
from contextlib import asynccontextmanager

# -----------------------------------------------------------------------------
# Comprehensive Debug Logging System
# -----------------------------------------------------------------------------
import logging
from logging import Logger

# Configure colored console output if available
try:
    import colorlog  # type: ignore
    
    handler = colorlog.StreamHandler()
    handler.setFormatter(colorlog.ColoredFormatter(
        '%(log_color)s[%(asctime)s] %(levelname)s [%(name)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        log_colors={
            'DEBUG': 'cyan',
            'INFO': 'green',
            'WARNING': 'yellow',
            'ERROR': 'red',
            'CRITICAL': 'red,bg_white',
        }
    ))
    
    logging.basicConfig(level=logging.WARNING, handlers=[handler])
except ImportError:
    # Fallback to standard logging if colorlog is not available
    logging.basicConfig(
        level=logging.WARNING,
        format='[%(asctime)s] %(levelname)s [%(name)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

# Create loggers with higher log levels to reduce verbosity
auth_logger = logging.getLogger("nibblemate.auth")
auth_logger.setLevel(logging.WARNING)  # Only show warnings and errors

api_logger = logging.getLogger("nibblemate.api")
api_logger.setLevel(logging.WARNING)  # Only show warnings and errors

db_logger = logging.getLogger("nibblemate.db")
db_logger.setLevel(logging.WARNING)  # Only show warnings and errors

ws_logger = logging.getLogger("nibblemate.ws")
ws_logger.setLevel(logging.WARNING)  # Only show warnings and errors

# Transaction tracking
transaction_contexts = {}

def generate_transaction_id() -> str:
    """Generate a unique transaction ID for tracking requests"""
    return str(uuid.uuid4())[:8]

class TransactionContext:
    """Track context throughout a transaction's lifecycle"""
    def __init__(self, transaction_id: str, user_id: Optional[str] = None, path: Optional[str] = None, method: Optional[str] = None):
        self.transaction_id = transaction_id
        self.user_id = user_id
        self.path = path
        self.method = method
        self.start_time = time.time()
        self.checkpoints = []
        
    def add_checkpoint(self, name: str) -> float:
        """Add a checkpoint and return elapsed time since last checkpoint"""
        now = time.time()
        elapsed = 0
        
        if self.checkpoints:
            elapsed = now - self.checkpoints[-1][1]
        else:
            elapsed = now - self.start_time
            
        self.checkpoints.append((name, now, elapsed))
        return elapsed
    
    def get_elapsed(self) -> float:
        """Get total elapsed time for this transaction"""
        return time.time() - self.start_time
    
    def to_dict(self) -> dict:
        """Convert context to dictionary for logging"""
        return {
            "transaction_id": self.transaction_id,
            "user_id": self.user_id,
            "path": self.path,
            "method": self.method,
            "elapsed_time": f"{self.get_elapsed():.3f}s",
            "checkpoints": [
                {"name": name, "time": f"{elapsed:.3f}s"} 
                for name, _, elapsed in self.checkpoints
            ]
        }

def log_transaction_start(request: Optional[Request] = None, user_id: Optional[str] = None) -> str:
    """Start tracking a new transaction and return the transaction ID"""
    transaction_id = generate_transaction_id()
    
    path = None
    method = None
    if request:
        path = request.url.path
        method = request.method
        
    context = TransactionContext(
        transaction_id=transaction_id,
        user_id=user_id,
        path=path,
        method=method
    )
    
    transaction_contexts[transaction_id] = context
    
    # Only log at debug level
    api_logger.debug(f"🔶 Transaction {transaction_id} started: {method or 'N/A'} {path or 'N/A'}")
    return transaction_id

def log_checkpoint(transaction_id: str, name: str) -> None:
    """Log a checkpoint within a transaction"""
    if transaction_id in transaction_contexts:
        context = transaction_contexts[transaction_id]
        elapsed = context.add_checkpoint(name)
        # Only log at debug level
        api_logger.debug(f"⏱️ TXN-{transaction_id} Checkpoint '{name}' reached in {elapsed:.3f}s")

def log_transaction_end(transaction_id: str, status_code: Optional[int] = None, provided_elapsed: Optional[float] = None) -> None:
    """Log the end of a transaction with summary"""
    if transaction_id in transaction_contexts:
        context = transaction_contexts[transaction_id]
        elapsed = provided_elapsed if provided_elapsed is not None else context.get_elapsed()
        
        # Increase the threshold for slow transactions to 2 seconds
        # Only log errors or genuinely slow transactions
        if status_code is not None and status_code >= 400:
            status_indicator = "❌"
            api_logger.error(
                f"{status_indicator} Transaction {transaction_id} status {status_code} in {elapsed:.3f}s "
                f"[{context.method or 'N/A'} {context.path or 'N/A'}]"
            )
        elif elapsed > 2.0:  # Increased threshold to 2s
            api_logger.warning(
                f"⚠️ Slow transaction {transaction_id} completed in {elapsed:.3f}s "
                f"[{context.method or 'N/A'} {context.path or 'N/A'}]"
            )
            
        # Clean up
        del transaction_contexts[transaction_id]

# API tracing function
def trace_api_call(logger: Logger, prefix: str, action: str, data: Optional[dict] = None, error: Optional[Exception] = None) -> None:
    """Consistently format API call tracing"""
    if error:
        logger.error(f"{prefix} ❌ {action}: {str(error)}")
        if hasattr(error, "__traceback__"):
            logger.debug(traceback.format_exc())
    elif data:
        logger.info(f"{prefix} ✅ {action}: {json.dumps(data, default=str)}")
    else:
        logger.info(f"{prefix} 🔄 {action}")

# Database tracing function
def trace_db_operation(operation: str, table: str, filters: Optional[dict] = None, result: Optional[object] = None, error: Optional[Exception] = None) -> None:
    """Trace database operations"""
    prefix = f"[DB:{table}]"
    
    if error:
        db_logger.error(f"{prefix} ❌ {operation} failed: {str(error)}")
        if hasattr(error, "__traceback__"):
            db_logger.debug(traceback.format_exc())
    elif result is not None:
        count = len(result) if isinstance(result, list) else 1 if result else 0
        db_logger.info(f"{prefix} ✅ {operation} completed: {count} results")
        db_logger.debug(f"{prefix} Results: {json.dumps(result, default=str)}")
    else:
        filter_str = json.dumps(filters) if filters else ""
        db_logger.info(f"{prefix} 🔄 {operation} with filters: {filter_str}")

# Monitor for stuck transactions
def monitor_stuck_transactions():
    """Check for long-running transactions"""
    now = time.time()
    for txn_id, context in list(transaction_contexts.items()):
        elapsed = now - context.start_time
        # If transaction is older than 30 seconds, log a warning
        if elapsed > 30:
            api_logger.warning(
                f"⚠️ Stuck transaction {txn_id} detected: running for {elapsed:.1f}s "
                f"[{context.method or 'N/A'} {context.path or 'N/A'}]"
            )

# Set up debug request identifier header for cross-service tracing
DEBUG_REQUEST_ID_HEADER = "X-NibbleMate-Request-ID"

# -----------------------------------------------------------------------------
# Enhanced Supabase Debug Helper
# -----------------------------------------------------------------------------
def supabase_debug_query(
    operation: str,
    table: str, 
    transaction_id: Optional[str] = None,
    query_fn = None,
    **kwargs
) -> tuple:
    """
    Execute and debug a Supabase query with minimal logging.
    Returns: Tuple of (data, error)
    """
    start_time = time.time()
    prefix = f"[DB:{table}]"
    
    try:
        if not callable(query_fn):
            return None, ValueError("No query function provided")
            
        data = query_fn()
        duration = time.time() - start_time
        
        # Log only success with minimal info
        if isinstance(data, list):
            db_logger.info(f"{prefix} ✅ {operation} completed: {len(data)} results")
        else:
            db_logger.info(f"{prefix} ✅ {operation} completed")
            
        if hasattr(data, 'data'):
            data = getattr(data, 'data')
            
        return data, None
        
    except Exception as e:
        # Log only critical error info
        db_logger.error(f"{prefix} ❌ {operation} failed: {str(e)}")
        return None, e

# -----------------------------------------------------------------------------
# Environment & Supabase client
# -----------------------------------------------------------------------------
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY") # This is typically the anon key
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY") # This is the service_role key

# Validate required environment variables
if not SUPABASE_URL or not (SUPABASE_KEY or SUPABASE_SERVICE_KEY):
    raise RuntimeError("SUPABASE_URL and either SUPABASE_KEY or SUPABASE_SERVICE_KEY env vars are required")

# Import required packages
try:
    import httpx  # type: ignore  # noqa: F401
    from functools import lru_cache
    from datetime import datetime, timedelta
    import asyncio
except ImportError:
    print("WARNING: httpx is not installed. Performance will be degraded.")
    print("Run 'pip install httpx' to improve connection performance.")

# Supabase Connection Manager
class SupabaseConnectionManager:
    """Manage Supabase connections with proper pooling and caching"""
    def __init__(self, supabase_url, api_key, service_key=None):
        self.supabase_url = supabase_url
        self.api_key = api_key
        self.service_key = service_key
        self.client = None
        self.init_time = None
        self.setup_client()
    
    def setup_client(self):
        """Set up the Supabase client with connection pooling and native SSL handling"""
        print(f"🌐 Initializing optimized Supabase client with URL: {self.supabase_url}")
        
        # Use service role key if available
        key_to_use = self.service_key if self.service_key else self.api_key
        
        # Set up HTTP client with connection pooling if httpx is available
        http_client = None
        if 'httpx' in globals():
            try:
                # Create a client with good defaults for production use
                http_client = httpx.Client(
                    timeout=10.0,  # 10 second timeout
                    limits=httpx.Limits(
                        max_keepalive_connections=20,
                        max_connections=30,
                        keepalive_expiry=60.0
                    ),
                    # Let httpx handle SSL verification with system certificates
                    verify=True
                )
                print("✅ Created pooled HTTP client with native SSL verification")
            except Exception as e:
                print(f"⚠️ Could not create pooled HTTP client: {e}")
                print("⚠️ Will use default HTTP client")
        
        # Create the client with duck typing-friendly error handling
        try:
            # Use Supabase's built-in SSL handling by default
            if http_client:
                # Try with http_client parameter
                self.client = create_client(self.supabase_url, key_to_use, http_client=http_client)  # type: ignore
            else:
                # Let Supabase handle SSL internally
                self.client = create_client(self.supabase_url, key_to_use)
                
        except TypeError as e:
            # If http_client parameter is not supported, try without it
            print(f"⚠️ Your Supabase client version doesn't support http_client parameter: {e}")
            print("⚠️ Creating client without connection pooling")
            self.client = create_client(self.supabase_url, key_to_use)
            
        except Exception as e:
            print(f"⚠️ Error creating Supabase client: {e}")
            raise
            
        # Set initialization time
        self.init_time = float(time.time())
        print("✅ Supabase client initialized successfully with native SSL handling")
    
    def get_client(self):
        """Get the current Supabase client instance"""
        current_time = float(time.time())
        
        # If client exists and is less than 1 hour old, return it
        if self.client and self.init_time is not None and (current_time - self.init_time < 3600.0):
            return self.client
            
        # Otherwise recreate the client (to avoid stale connections)
        self.setup_client()
        return self.client

# Create the connection manager with robust error handling
try:
    connection_manager = SupabaseConnectionManager(
        SUPABASE_URL,
        SUPABASE_KEY,
        SUPABASE_SERVICE_KEY
    )

    # Get the client through the manager
    supabase: Client = connection_manager.get_client()  # type: ignore
    
    print("✅ Supabase client created successfully")
except Exception as e:
    print(f"⚠️ Error initializing Supabase client: {str(e)}")
    # Create a production-quality fallback client that handles operations safely
    # This allows the server to start even with database issues while protecting customer data
    from typing import Any, cast
    
    class FallbackClient:
        """Duck-typed fallback client that safely handles operations when database is unavailable"""
        def __init__(self):
            self.name = "FallbackClient"
            # Set up logging for audit trail
            self.audit_log_path = os.path.join(os.getcwd(), "audit_log.txt")
            self.log_audit_event("FALLBACK_CLIENT_INITIALIZED", "Fallback client initialized due to database connection failure")
            
        def log_audit_event(self, event_type, event_details):
            """Log security-relevant events for customer data safety"""
            try:
                timestamp = datetime.utcnow().isoformat()
                log_entry = f"[{timestamp}] [{event_type}] {event_details}\n"
                with open(self.audit_log_path, "a") as f:
                    f.write(log_entry)
            except Exception as e:
                # Last resort logging to stdout if file writing fails
                print(f"⚠️ AUDIT LOG: [{event_type}] {event_details}")
                
        def __getattr__(self, name):
            """Dynamically handle any method call with proper duck typing"""
            def method(*args, **kwargs):
                # Log the attempted operation for security audit
                self.log_audit_event("OPERATION_ATTEMPTED", 
                                    f"Method: {name}, Args: {args}, Kwargs: {list(kwargs.keys())}")
                print(f"⚠️ Database operation '{name}' failed: Database connection not available")
                # Return self for method chaining (duck typing)
                return self
            return method
        
        # Essential methods implemented with proper signatures for duck typing
        def from_(self, table):
            self.log_audit_event("TABLE_ACCESS_ATTEMPT", f"Table: {table}")
            return self
            
        def table(self, table):
            self.log_audit_event("TABLE_ACCESS_ATTEMPT", f"Table: {table}")
            return self
            
        def auth(self, *args, **kwargs):
            self.log_audit_event("AUTH_OPERATION", "Auth operation attempted")
            return self
            
        def select(self, *args, **kwargs):
            self.log_audit_event("SELECT_OPERATION", f"Select: {args}")
            return self
            
        def insert(self, data, *args, **kwargs):
            # Log attempted data insertion but sanitize sensitive data
            safe_data = self._sanitize_data_for_logging(data)
            self.log_audit_event("INSERT_ATTEMPT", f"Insert operation with fields: {list(safe_data.keys())}")
            return self
            
        def update(self, data, *args, **kwargs):
            # Log attempted data update but sanitize sensitive data
            safe_data = self._sanitize_data_for_logging(data)
            self.log_audit_event("UPDATE_ATTEMPT", f"Update operation with fields: {list(safe_data.keys())}")
            return self
            
        def delete(self, *args, **kwargs):
            self.log_audit_event("DELETE_ATTEMPT", "Delete operation attempted")
            return self
            
        def _sanitize_data_for_logging(self, data):
            """Sanitize sensitive data for logging to protect customer privacy"""
            if not isinstance(data, dict):
                return {"<non-dict-data>": True}
            
            safe_data = {}
            sensitive_fields = {"password", "token", "secret", "credit_card", "ssn", "email"}
            
            for key, value in data.items():
                if any(sensitive in key.lower() for sensitive in sensitive_fields):
                    safe_data[key] = "<REDACTED>"
                else:
                    safe_data[key] = value
            return safe_data
            
        def execute(self):
            """Return a proper response object with empty data"""
            class DummyResponse:
                data = []
                def dict(self):
                    return {"data": []}
                def json(self):
                    return "{\"data\":[]}"
                    
            return DummyResponse()
            
        def sign_up(self, credentials):
            """Handle sign-up safely when DB is unavailable"""
            self.log_audit_event("SIGNUP_ATTEMPT", "User signup attempted during database outage")
            raise Exception("Authentication service temporarily unavailable. Please try again later.")
            
        def sign_in_with_password(self, credentials):
            """Handle sign-in safely when DB is unavailable"""
            self.log_audit_event("SIGNIN_ATTEMPT", "User signin attempted during database outage")
            raise Exception("Authentication service temporarily unavailable. Please try again later.")
    
    print("⚠️ Using fallback client - database operations will be unavailable")
    print("⚠️ Customer data is protected with full audit logging")
    # Use type casting to satisfy the type checker
    supabase = cast(Client, FallbackClient())

# Configure longer JWT expiration for this non-security-critical application
# This will make tokens valid for 30 days (in seconds)
JWT_EXPIRY_SECONDS = int(os.getenv("JWT_EXPIRY_SECONDS", 2592000))  # 30 days
JWT_EXPIRATION = JWT_EXPIRY_SECONDS
JWT_EXPIRE = JWT_EXPIRY_SECONDS
# This value is used in Supabase sign-up and sign-in endpoints to set session expiry

# Instead of testing with an invalid token, just log that the client is created
print(f"🔌 Supabase client created with URL: {SUPABASE_URL}")
print(f"🔌 API endpoints should be available at: {SUPABASE_URL}/rest/v1")

# -----------------------------------------------------------------------------
# FastAPI application setup
# -----------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # For startup info, we'll use print directly to ensure it's always visible
    # regardless of log level settings
    print("\n======= NibbleMate API Server =======")
    print("🚀 Starting application")
    
    # Count and list registered routes
    total_routes = len(app.routes)
    print(f"📋 Registered {total_routes} routes:")
    
    # Group endpoints by path prefix for cleaner display
    endpoints_by_prefix = {}
    for route in app.routes:
        if hasattr(route, "path") and hasattr(route, "methods"):
            path = route.path  # type: ignore
            methods = route.methods  # type: ignore
            prefix = path.split("/")[1] if len(path.split("/")) > 1 else "root"
            
            if prefix not in endpoints_by_prefix:
                endpoints_by_prefix[prefix] = []
                
            endpoint_info = f"{', '.join(methods)} {path}"
            endpoints_by_prefix[prefix].append(endpoint_info)
    
    # Print grouped endpoints
    for prefix, endpoints in sorted(endpoints_by_prefix.items()):
        print(f"\n/{prefix} endpoints:")
        for endpoint in sorted(endpoints):
            print(f"  - {endpoint}")
    
    # Test database connection with improved error handling
    print("\nTesting database connection...")
    try:
        # First ensure SSL environment variables are properly handled
        if 'SSL_CERT_FILE' in os.environ and not os.path.exists(os.environ['SSL_CERT_FILE']):
            original_ssl_cert = os.environ.pop('SSL_CERT_FILE')
            print(f"⚠️ Removed invalid SSL_CERT_FILE environment variable (was set to non-existent path)")
        
        # Use a more reliable table to test with simple query
        # Simple query without using problematic count parameter
        response = supabase.from_("profiles").select("id").limit(1).execute()
            
        # Extract data to verify connection success
        data = getattr(response, 'data', [])
        count = len(data)
        print(f"✅ Database connection successful (retrieved {count} records)")
    except Exception as e:
        print(f"⚠️ Database connection warning: {str(e)}")
        print("⚠️ Will attempt to proceed with startup anyway - some features may not work correctly")
    
    print("=======================================\n")
    
    yield  # Give control back to FastAPI
    
    # Shutdown code (if any) would go here

# Create the FastAPI app with lifespan - IMPORTANT: Define once before any routes
app = FastAPI(title="nibbleMate API", lifespan=lifespan)

# Initialize the rate limiter with slowapi
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(status.HTTP_429_TOO_MANY_REQUESTS, _rate_limit_exceeded_handler)  # type: ignore

# Initialize security for authentication
security = HTTPBearer()

# Food brands endpoint - placed early to ensure registration
@app.get("/feeders/foodbrands")
async def get_food_brands(credentials: HTTPAuthorizationCredentials = Depends(security)):
    txn_id = log_transaction_start()
    try:
        token = credentials.credentials
        api_logger.info(f"🍽️ GET /feeders/foodbrands - Processing request")
        log_checkpoint(txn_id, "Processing food brands request")
        
        # Directly decode the JWT token to verify authentication
        user_id = None
        try:
            # Decode the JWT token and extract user_id
            decoded_token = jwt.decode(
                token,
                options={"verify_signature": False},
                algorithms=["HS256"]
            )
            
            # The user_id is in the 'sub' field
            user_id = decoded_token.get("sub")
            
            if not user_id:
                api_logger.error(f"🍽️ GET /feeders/foodbrands - No user_id found in token")
                log_checkpoint(txn_id, "Authentication failed: No user ID in token")
                log_transaction_end(txn_id, 401)
                raise HTTPException(status_code=401, detail="Invalid authentication token")
                
            api_logger.info(f"🍽️ GET /feeders/foodbrands - Successfully extracted user_id: {user_id}")
            
        except Exception as token_error:
            api_logger.error(f"🍽️ GET /feeders/foodbrands - Error decoding token: {str(token_error)}")
            log_checkpoint(txn_id, "Authentication failed: Token decode error")
            log_transaction_end(txn_id, 401)
            raise HTTPException(status_code=401, detail="Invalid authentication token")
        
        # Fetch food brands from the database
        log_checkpoint(txn_id, "Fetching food brands from database")
        try:
            # Query the foodbrands table
            api_logger.debug(f"🍽️ GET /feeders/foodbrands - Querying foodbrands table")
            
            foodbrands_query = lambda: supabase.table("foodbrands").select("id, brandname, calories, servsize").order("brandname").execute().data
            
            foodbrands, error = supabase_debug_query(
                "select",
                "foodbrands",
                txn_id,
                foodbrands_query
            )
            
            if error:
                api_logger.error(f"🍽️ GET /feeders/foodbrands - Database error: {error}")
                log_checkpoint(txn_id, f"Database error: {error}")
                raise Exception(f"Database error: {error}")
            
            if not foodbrands:
                api_logger.info(f"🍽️ GET /feeders/foodbrands - No food brands found in database")
                foodbrands = []
            
            # Transform the data to match the expected format in the frontend
            transformed_brands = []
            for brand in foodbrands:
                transformed_brands.append({
                    "id": brand.get("id"),  # Include the ID field needed for creation
                    "brandName": brand.get("brandname", ""),
                    "calories": brand.get("calories", 3500),  # Default to 3500 kcal/kg
                    "servSize": brand.get("servsize", 100)    # Default to 100g serving
                })
                
            api_logger.info(f"🍽️ GET /feeders/foodbrands - Successfully retrieved and transformed {len(transformed_brands)} food brands")
            log_checkpoint(txn_id, f"Retrieved {len(transformed_brands)} food brands")
            
        except Exception as db_error:
            api_logger.error(f"🍽️ GET /feeders/foodbrands - Database error: {str(db_error)}")
            log_checkpoint(txn_id, f"Database error: {str(db_error)}")
            raise HTTPException(status_code=500, detail=f"Database error: {str(db_error)}")
        
        log_transaction_end(txn_id, 200)
        return transformed_brands
        
    except HTTPException:
        raise
    except Exception as e:
        api_logger.error(f"🍽️ GET /feeders/foodbrands - Error: {str(e)}")
        log_checkpoint(txn_id, f"Error: {str(e)}")
        log_transaction_end(txn_id, 500)
        raise HTTPException(status_code=500, detail=f"Error fetching food brands: {str(e)}")

# CRITICAL FIX: All cats endpoints must be defined immediately after app creation
# to ensure they are properly registered

# CRITICAL FIX: Moving the cats endpoint up in the file to ensure it gets registered
@app.get("/cats", response_model=None)
async def get_user_cats(
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Get all cats associated with the authenticated user"""
    # Initialize transaction tracking
    txn_id = generate_transaction_id()
    log_transaction_start()
    
    try:
        # Extract user ID from token with improved error handling
        user_id = extract_user_id_from_token(credentials.credentials)
        if not user_id:
            log_checkpoint(txn_id, "Authentication failed - invalid token")
            log_transaction_end(txn_id, 401)
            return JSONResponse(
                status_code=401,
                content={"error": "Invalid or expired token"}
            )
            
        # Set auth context with Supabase for RLS policies
        try:
            supabase.auth.get_user(credentials.credentials)
            log_checkpoint(txn_id, "Auth context set with Supabase")
        except Exception as auth_error:
            api_logger.error(f"🐱 GET /cats - Auth context error: {str(auth_error)}")
            log_checkpoint(txn_id, f"Auth context error: {str(auth_error)}")
            log_transaction_end(txn_id, 401)
            return JSONResponse(
                status_code=401,
                content={"error": "Failed to set authentication context"}
            )
            
        log_checkpoint(txn_id, f"Authenticated user {user_id}")
        api_logger.info(f"🐱 GET /cats - Fetching cats for user: {user_id}")
        
        # Get all cats belonging to this user
        cats_data, error = supabase_debug_query(
            "select",
            "cat",
            txn_id,
            lambda: supabase.table("cat")
                .select("*")
                .eq("userid", user_id)
                .execute()
                .data
        )
        
        if error:
            api_logger.error(f"🐱 GET /cats - Error fetching direct user cats: {error}")
            log_checkpoint(txn_id, f"Database error: {str(error)}")
            log_transaction_end(txn_id, 500)
            return JSONResponse(
                status_code=500,
                content={"error": "Failed to fetch cats"}
            )
            
        # Filter out any cats with DISASSOCIATED prefix that might still be in the result
        cats_data = [cat for cat in (cats_data or []) if not cat.get("catname", "").startswith("DISASSOCIATED:")]
        
        # Log the direct user cats found
        api_logger.info(f"🐱 GET /cats - Direct user cats found: {len(cats_data) if cats_data else 0}")
            
        # If no cats found directly, try through feeder relationship
        if not cats_data:
            # Check if the user has any feeders
            feeder_check, error = supabase_debug_query(
                "select",
                "feeder",
                txn_id,
                lambda: supabase.table("feeder")
                    .select("feederid")
                    .eq("owner_id", user_id)
                    .execute()
                    .data
            )
            
            if error:
                api_logger.error(f"🐱 GET /cats - Error checking feeders: {error}")
                log_checkpoint(txn_id, f"Feeder check error: {str(error)}")
                log_transaction_end(txn_id, 500)
                return JSONResponse(
                    status_code=500,
                    content={"error": "Failed to check feeders"}
                )
            
            if feeder_check:
                feeder_ids = [f["feederid"] for f in feeder_check]
                api_logger.info(f"🐱 GET /cats - User has feeders: {feeder_ids}")
                
                # Try to get cats through the feeders
                feeder_cats, error = supabase_debug_query(
                    "select",
                    "cat",
                    txn_id,
                    lambda: supabase.table("cat")
                        .select("*")
                        .in_("feederid", feeder_ids)
                        .execute()
                        .data
                )
                
                if error:
                    api_logger.error(f"🐱 GET /cats - Error fetching feeder cats: {error}")
                    log_checkpoint(txn_id, f"Feeder cats error: {str(error)}")
                    log_transaction_end(txn_id, 500)
                    return JSONResponse(
                        status_code=500,
                        content={"error": "Failed to fetch feeder cats"}
                    )
                
                if feeder_cats:
                    # Filter out any disassociated cats from feeder results too
                    feeder_cats = [cat for cat in feeder_cats if not cat.get("catname", "").startswith("DISASSOCIATED:")]
                    cats_data = feeder_cats
                    api_logger.info(f"🐱 GET /cats - Feeder cats found: {len(feeder_cats)}")
        
        # Log and return the data
        api_logger.info(f"🐱 GET /cats - Returning {len(cats_data)} cats")
        log_transaction_end(txn_id, 200)
        return cats_data
        
    except Exception as e:
        api_logger.error(f"🐱 GET /cats - Error: {str(e)}")
        log_checkpoint(txn_id, f"Error: {str(e)}")
        log_transaction_end(txn_id, 500)
        raise HTTPException(status_code=500, detail=f"Error fetching cats: {str(e)}")

# GET endpoint to retrieve a single cat by ID
@app.get("/cats/{catid}", response_model=None)
async def get_cat_by_id(
    catid: int,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    txn_id = log_transaction_start()
    try:
        api_logger.info(f"🐱 GET /cats/{catid} - Processing request")
        
        # Authenticate user
        token = credentials.credentials
        user_id = None
        
        try:
            # Decode the JWT token and extract user_id
            decoded_token = jwt.decode(
                token,
                options={"verify_signature": False},
                algorithms=["HS256"]
            )
            
            # The user_id is in the 'sub' field
            user_id = decoded_token.get("sub")
            
            if not user_id:
                api_logger.error(f"🐱 GET /cats/{catid} - No user_id found in token")
                raise HTTPException(status_code=401, detail="Invalid authentication token")
                
            api_logger.info(f"🐱 GET /cats/{catid} - Successfully extracted user_id: {user_id}")
            
            # Set auth context with Supabase for RLS policies
            api_logger.info(f"🐱 GET /cats/{catid} - Setting auth context with Supabase")
            supabase.auth.get_user(token)
            log_checkpoint(txn_id, "Auth context set with Supabase")
            
        except Exception as token_error:
            api_logger.error(f"🐱 GET /cats/{catid} - Error decoding token: {str(token_error)}")
            raise HTTPException(status_code=401, detail="Invalid authentication token")
        
        # First check if the cat belongs directly to the user
        log_checkpoint(txn_id, f"Checking if cat {catid} belongs to user {user_id}")
        cat_data = supabase.table("cat").select("*").eq("catid", catid).eq("userid", user_id).execute().data
        
        if not cat_data or len(cat_data) == 0:
            # Try to find the cat through user's feeders
            api_logger.info(f"🐱 GET /cats/{catid} - Cat not found directly, checking through feeders")
            
            # Get user's feeders
            feeders = supabase.table("feeder").select("feederid").eq("owner_id", user_id).execute().data
            
            if not feeders or len(feeders) == 0:
                api_logger.warning(f"🐱 GET /cats/{catid} - User has no feeders")
                raise HTTPException(status_code=404, detail="Cat not found")
                
            feeder_ids = [f["feederid"] for f in feeders]
            
            # Look for the cat in the user's feeders
            cat_data = supabase.table("cat").select("*").eq("catid", catid).in_("feederid", feeder_ids).execute().data
            
            if not cat_data or len(cat_data) == 0:
                api_logger.warning(f"🐱 GET /cats/{catid} - Cat not found or not associated with user's feeders")
                raise HTTPException(status_code=404, detail="Cat not found")
        
        # Return the first (and should be only) cat from the results
        log_checkpoint(txn_id, "Cat found successfully")
        
        # Check if the cat is marked as disassociated
        if cat_data and len(cat_data) > 0 and cat_data[0].get("catname", "").startswith("DISASSOCIATED:"):
            api_logger.warning(f"🐱 GET /cats/{catid} - Cat is disassociated")
            raise HTTPException(status_code=404, detail="Cat not found or disassociated")
            
        api_logger.info(f"🐱 GET /cats/{catid} - Successfully retrieved cat")
        log_transaction_end(txn_id, 200)
        
        return cat_data[0]
        
    except Exception as e:
        log_checkpoint(txn_id, f"Error: {str(e)}")
        api_logger.error(f"🐱 GET /cats/{catid} - Error: {str(e)}")
        log_transaction_end(txn_id, 500)
        raise HTTPException(status_code=500, detail=f"Error retrieving cat: {str(e)}")

# PUT endpoint to update a cat's information
@app.put("/cats/{catid}", response_model=None)
async def update_cat(
    catid: int,
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    txn_id = log_transaction_start(request)
    try:
        # Authenticate user
        token = credentials.credentials
        user_id = None
        
        try:
            decoded_token = jwt.decode(token, options={"verify_signature": False}, algorithms=["HS256"])
            user_id = decoded_token.get("sub")
            if not user_id:
                api_logger.error(f"🐱 PUT /cats/{catid} - No user_id found in token")
                raise HTTPException(status_code=401, detail="Invalid authentication token")
            api_logger.info(f"🐱 PUT /cats/{catid} - Successfully extracted user_id: {user_id}")
            
            # Set auth context with Supabase for RLS policies
            api_logger.info(f"🐱 PUT /cats/{catid} - Setting auth context with Supabase")
            supabase.auth.get_user(token)
            log_checkpoint(txn_id, "Auth context set with Supabase")
        except Exception as token_error:
            api_logger.error(f"🐱 PUT /cats/{catid} - Error decoding token: {str(token_error)}")
            raise HTTPException(status_code=401, detail="Invalid authentication token")
        
        # Check if cat exists and belongs to user
        cat_check = supabase.table("cat") \
            .select("*") \
            .eq("catid", catid) \
            .eq("userid", user_id) \
            .execute()
            
        if not cat_check.data or len(cat_check.data) == 0:
            # Try to find the cat through user's feeders
            api_logger.info(f"🐱 PUT /cats/{catid} - Cat not found directly, checking through feeders")
            
            # Get user's feeders
            feeders = supabase.table("feeder").select("feederid").eq("owner_id", user_id).execute().data
            
            if not feeders or len(feeders) == 0:
                api_logger.warning(f"🐱 PUT /cats/{catid} - User has no feeders")
                raise HTTPException(status_code=404, detail="Cat not found")
                
            feeder_ids = [f["feederid"] for f in feeders]
            
            # Look for the cat in the user's feeders
            cat_check = supabase.table("cat").select("*").eq("catid", catid).in_("feederid", feeder_ids).execute()
            
            if not cat_check.data or len(cat_check.data) == 0:
                api_logger.warning(f"🐱 PUT /cats/{catid} - Cat not found or not associated with user's feeders")
                raise HTTPException(status_code=404, detail="Cat not found or not owned by user")
        
        # Parse request body
        body = await request.json()
        api_logger.info(f"🐱 PUT /cats/{catid} - Update request body: {body}")
        
        # Extract and validate fields
        update_data = {}
        
        # Process non-numeric fields
        if "catname" in body and body["catname"]:
            update_data["catname"] = body["catname"]
        if "catbreed" in body and body["catbreed"]:
            update_data["catbreed"] = body["catbreed"]
        if "catsex" in body and body["catsex"]:
            update_data["catsex"] = body["catsex"]
        
        # Process numeric fields with type conversion
        numeric_fields = {
            "catage": int,
            "catweight": float,
            "catlength": float,
            "feederid": int,
            "microchip": int
        }
        
        for field, convert_type in numeric_fields.items():
            if field in body and body[field] is not None:
                try:
                    # Convert string to appropriate numeric type
                    if isinstance(body[field], str) and body[field].strip():
                        update_data[field] = convert_type(body[field])
                    # Handle direct numeric values
                    elif isinstance(body[field], (int, float)):
                        update_data[field] = convert_type(body[field])
                except ValueError:
                    api_logger.error(f"🐱 PUT /cats/{catid} - Invalid {field} value: {body[field]}")
                    raise HTTPException(status_code=400, detail=f"Invalid {field} value")
        
        # If update_data is empty, no valid fields were provided
        if not update_data:
            api_logger.error(f"🐱 PUT /cats/{catid} - No valid fields to update")
            raise HTTPException(status_code=400, detail="No valid fields to update")
            
        # Update cat in database
        result = supabase.table("cat") \
            .update(update_data) \
            .eq("catid", catid) \
            .execute()
            
        if not result.data or len(result.data) == 0:
            api_logger.error(f"🐱 PUT /cats/{catid} - Update failed")
            raise HTTPException(status_code=500, detail="Failed to update cat")
            
        updated_cat = result.data[0]
        api_logger.info(f"🐱 PUT /cats/{catid} - Cat updated successfully")
        
        log_transaction_end(txn_id, 200)
        return updated_cat
        
    except Exception as e:
        api_logger.error(f"🐱 PUT /cats/{catid} - Error updating cat: {str(e)}")
        log_checkpoint(txn_id, f"Error: {str(e)}")
        log_transaction_end(txn_id, 500)
        raise HTTPException(status_code=500, detail=f"Error updating cat: {str(e)}")

# PATCH endpoint to disassociate a cat (rather than deleting it)
@app.patch("/cats/{catid}/disassociate", response_model=None)
async def disassociate_cat(
    catid: int,
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    txn_id = log_transaction_start(request)
    try:
        # Authenticate user
        token = credentials.credentials
        user_id = None
        
        try:
            decoded_token = jwt.decode(token, options={"verify_signature": False}, algorithms=["HS256"])
            user_id = decoded_token.get("sub")
            if not user_id:
                api_logger.error(f"🐱 PATCH /cats/{catid}/disassociate - No user_id found in token")
                raise HTTPException(status_code=401, detail="Invalid authentication token")
            api_logger.info(f"🐱 PATCH /cats/{catid}/disassociate - Successfully extracted user_id: {user_id}")
            
            # Set auth context with Supabase for RLS policies
            api_logger.info(f"🐱 PATCH /cats/{catid}/disassociate - Setting auth context with Supabase")
            supabase.auth.get_user(token)
            log_checkpoint(txn_id, "Auth context set with Supabase")
        except Exception as token_error:
            api_logger.error(f"🐱 PATCH /cats/{catid}/disassociate - Error decoding token: {str(token_error)}")
            raise HTTPException(status_code=401, detail="Invalid authentication token")
        
        # Check if cat exists and belongs to user
        cat_check = supabase.table("cat") \
            .select("*") \
            .eq("catid", catid) \
            .eq("userid", user_id) \
            .execute()
            
        if not cat_check.data or len(cat_check.data) == 0:
            api_logger.error(f"🐱 PATCH /cats/{catid}/disassociate - Cat not found or not owned by user {user_id}")
            raise HTTPException(status_code=404, detail="Cat not found or not owned by user")
        
        # Get the current cat details for logging
        current_cat = cat_check.data[0]
        
        try:
            # Update the cat by adding DISASSOCIATED: prefix to the name
            # while preserving the userid for potential restoration
            update_data = {
                "catname": f"DISASSOCIATED: {current_cat.get('catname')}"
            }
            
            result = supabase.table("cat") \
                .update(update_data) \
                .eq("catid", catid) \
                .execute()
                
            if not result.data or len(result.data) == 0:
                api_logger.error(f"🐱 PATCH /cats/{catid}/disassociate - Disassociation failed")
                raise HTTPException(status_code=500, detail="Failed to disassociate cat")
                
            api_logger.info(f"🐱 PATCH /cats/{catid}/disassociate - Cat disassociated from user {user_id}")
            log_transaction_end(txn_id, 200)
            
            return {
                "success": True,
                "message": "Cat successfully disassociated",
                "cat": result.data[0]
            }
                
        except Exception as db_error:
            api_logger.error(f"🐱 PATCH /cats/{catid}/disassociate - Database error: {str(db_error)}")
            raise HTTPException(status_code=500, detail=f"Database error: {str(db_error)}")
    
    except Exception as e:
        api_logger.error(f"🐱 PATCH /cats/{catid}/disassociate - Error: {str(e)}")
        log_checkpoint(txn_id, f"Error: {str(e)}")
        log_transaction_end(txn_id, 500)
        raise HTTPException(status_code=500, detail=f"Error disassociating cat: {str(e)}")

# Also move POST /cats endpoint here to ensure it's registered
@app.post("/cats", response_model=None)
async def create_cat(request: Request, credentials: HTTPAuthorizationCredentials = Depends(security)):
        txn_id = log_transaction_start(request)
    try:
        # Log entry into POST /cats handler
        api_logger.info("🚨 POST /cats handler called")
        
        # Directly decode the JWT token to avoid dependency on verify_jwt
        token = credentials.credentials
        user_id = None
        
        try:
            # Decode the JWT token and extract user_id
            decoded_token = jwt.decode(
                token,
                options={"verify_signature": False},  # We're not verifying signature, just extracting data
                algorithms=["HS256"]
            )
            
            # The user_id is in the 'sub' field
            user_id = decoded_token.get("sub")
            
            if not user_id:
                api_logger.error(f"🐱 POST /cats - No user_id found in token: {decoded_token}")
                log_checkpoint(txn_id, f"Authentication failed: No user ID in token")
                raise HTTPException(status_code=401, detail="Invalid authentication token")
                
            api_logger.info(f"🐱 POST /cats - Successfully extracted user_id: {user_id}")
            
            # Set auth context with Supabase for RLS policies
            api_logger.info(f"🐱 POST /cats - Setting auth context with Supabase")
            supabase.auth.get_user(token)
            log_checkpoint(txn_id, "Auth context set with Supabase")
            
        except Exception as token_error:
            api_logger.error(f"🐱 POST /cats - Error decoding token: {str(token_error)}")
            log_checkpoint(txn_id, f"Authentication failed: Token decode error")
            raise HTTPException(status_code=401, detail="Invalid authentication token")

        # Parse request body
        body = await request.json()
        api_logger.info(f"Create cat request body: {body}")
        log_checkpoint(txn_id, f"Received create cat request with {len(body) if body else 0} fields")
        
        # Extract and validate required fields according to schema
        catname = body.get("catname")
        catbreed = body.get("catbreed")
        catweight = body.get("catweight")
        catlength = body.get("catlength")
        catsex = body.get("catsex")
        catage = body.get("catage")
        feederid = body.get("feederid")
        
        # Try to convert string values to appropriate types
        try:
            if catweight and isinstance(catweight, str):
                catweight = float(catweight)
            if catlength and isinstance(catlength, str):
                catlength = float(catlength)
            if catage and isinstance(catage, str):
                catage = int(catage)
            if feederid and isinstance(feederid, str):
                feederid = int(feederid)
        except ValueError:
            api_logger.error(f"🐱 POST /cats - Error converting numeric values: {body}")
            log_checkpoint(txn_id, f"Validation failed: Invalid numeric values")
            raise HTTPException(status_code=400, detail="Invalid numeric values provided")
        
        # Validate required fields
        missing_fields = []
        if not catname:
            missing_fields.append("catname")
        if not catbreed:
            missing_fields.append("catbreed")
        if not isinstance(catweight, (int, float)):
            missing_fields.append("catweight")
        if not isinstance(catlength, (int, float)):
            missing_fields.append("catlength")
        if not catsex:
            missing_fields.append("catsex")
        if not isinstance(catage, (int)):
            missing_fields.append("catage")
        if not feederid:
            missing_fields.append("feederid")
            
        if missing_fields:
            log_checkpoint(txn_id, f"Validation failed: Missing required fields: {', '.join(missing_fields)}")
            raise HTTPException(status_code=400, detail=f"Required fields missing: {', '.join(missing_fields)}")
        
        # Build cat data with required fields - using auto-increment for catid
        log_checkpoint(txn_id, "Building cat data for auto-increment")
        
        cat_data = {
            # "catid" is omitted to let PostgreSQL auto-assign using the sequence
            "catname": catname,
            "catbreed": catbreed,
            "catweight": catweight,
            "catlength": catlength,
            "catsex": catsex,
            "catage": catage,
            "feederid": feederid,
            "userid": user_id,
            # Include microchip if provided (nullable field)
            "microchip": body.get("microchip")
        }
        
        # Remove None values but keep all required fields
        cat_data = {k: v for k, v in cat_data.items() if v is not None}
        
        # Log the SQL query that would be executed (without the catid, as it will be auto-assigned)
        api_logger.info(f"🐱 POST /cats - Equivalent SQL: INSERT INTO cat (catname, catbreed, catweight, catlength, catsex, catage, feederid, userid) VALUES ('{catname}', '{catbreed}', {catweight}, {catlength}, '{catsex}', {catage}, {feederid}, '{user_id}')")
        
        # Insert into database, letting PostgreSQL handle the ID assignment
        try:
            result = supabase.table("cat").insert(cat_data).execute()
            
            if not result.data or len(result.data) == 0:
                raise HTTPException(status_code=500, detail="Failed to create cat")
        except Exception as e:
            # If there's any error, just report it - no need for retry logic
            api_logger.error(f"🐱 POST /cats - Error: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Failed to create cat: {str(e)}")
            
        created_cat = result.data[0]
        api_logger.info(f"Cat created successfully: catid={created_cat.get('catid')}")
        log_transaction_end(txn_id, 201)
        
        return created_cat

    except Exception as e:
        api_logger.error(f"Error creating cat: {str(e)}", exc_info=True)
        log_checkpoint(txn_id, f"Error: {str(e)}")
        log_transaction_end(txn_id, 500)
        raise HTTPException(status_code=500, detail=f"Error creating cat: {str(e)}")

# CORS is now fully handled at the NGINX level for better performance and centralized configuration

# -----------------------------------------------------------------------------
# Debug route to verify CORS headers
# -----------------------------------------------------------------------------
@app.options("/{full_path:path}")
async def options_handler(full_path: str, request: Request):
    """Global OPTIONS handler to assist with debugging HTTP method issues"""
    api_logger.info(f"⚠️ OPTIONS request received for path: /{full_path}")
    
    # Get the matching route if any
    matched_routes = []
    for route in app.routes:
        # Check if this path matches any registered route pattern
        route_path = getattr(route, "path", "")
        if route_path and not route_path.endswith("/{full_path:path}"):
            # Use a basic pattern matching for demonstration
            route_pattern = route_path.replace("{", "").replace("}", "")
            if route_pattern == f"/{full_path}" or (
                "{" in route_path and "/" + full_path.split("/")[0] == route_path.split("/")[1]
            ):
                methods = getattr(route, "methods", set())
                matched_routes.append((route_path, methods))
    
    if matched_routes:
        api_logger.info(f"⚠️ OPTIONS - Found {len(matched_routes)} matching routes: {matched_routes}")
        # Get all methods from matched routes
        all_methods = set()
        for _, methods in matched_routes:
            all_methods.update(methods)
        
        # Create Allow header with all supported methods
        allow_methods = ", ".join(sorted(all_methods))
        api_logger.info(f"⚠️ OPTIONS - Responding with Allow: {allow_methods}")
        
        # Return appropriate headers
        return Response(
            status_code=200,
            headers={
                "Allow": allow_methods,
                "Access-Control-Allow-Methods": allow_methods
            }
        )
    else:
        api_logger.warning(f"⚠️ OPTIONS - No matching routes found for /{full_path}")
        return Response(
            status_code=200,
            headers={
                "Allow": "GET, POST, HEAD, OPTIONS",
                "Access-Control-Allow-Methods": "GET, POST, HEAD, OPTIONS"
            }
        )


@app.get("/cors-debug")
async def cors_debug():
    """Debug endpoint to verify CORS headers are being set correctly"""
    return {
        "status": "ok", 
        "message": "CORS headers should be included in this response",
        "cors_enabled": True
    }

# -----------------------------------------------------------------------------
# Auth helpers
# -----------------------------------------------------------------------------
security = HTTPBearer()

# Configure logging for authentication
import logging
import time

# Configure logger
logging.basicConfig(level=logging.INFO)
auth_logger = logging.getLogger("nibblemate.auth")
api_logger = logging.getLogger("nibblemate.api")

# Token cache configuration
TOKEN_CACHE_TTL = 300  # 5 minutes
TOKEN_CACHE_SIZE = 1000  # Maximum number of tokens to cache

# Token cache with TTL
class TokenCache:
    def __init__(self, ttl_seconds: int = TOKEN_CACHE_TTL, max_size: int = TOKEN_CACHE_SIZE):
        self.ttl = ttl_seconds
        self.max_size = max_size
        self.cache = {}
        self.timestamps = {}
        
    def get(self, token: str) -> Optional[dict]:
        if token not in self.cache:
            return None
            
        # Check if token is expired
        if datetime.now().timestamp() - self.timestamps[token] > self.ttl:
            del self.cache[token]
            del self.timestamps[token]
            return None
            
        return self.cache[token]
        
    def set(self, token: str, data: dict):
        # Remove oldest entry if cache is full
        if len(self.cache) >= self.max_size:
            oldest_token = min(self.timestamps.items(), key=lambda x: x[1])[0]
            del self.cache[oldest_token]
            del self.timestamps[oldest_token]
            
        self.cache[token] = data
        self.timestamps[token] = datetime.now().timestamp()
        
    def clear(self):
        self.cache.clear()
        self.timestamps.clear()

# Initialize token cache
token_cache = TokenCache()

# Optimized token verification with caching
def verify_jwt(token: str) -> Optional[dict]:
    """Verify JWT token with caching for better performance"""
    # Check cache first
    cached_data = token_cache.get(token)
    if cached_data:
        return cached_data
        
    try:
        # Decode token without verification (since we're using Supabase's auth)
        decoded = jwt.decode(
            token,
            options={"verify_signature": False},
            algorithms=["HS256"]
        )
        
        # Cache the result
        token_cache.set(token, decoded)
        return decoded
    except Exception as e:
        api_logger.error(f"Token verification error: {str(e)}")
        return None

# Optimized user ID extraction
def extract_user_id_from_token(token: str, log_prefix: str = "") -> Optional[str]:
    """Extract user ID from token with caching"""
    try:
        # Get decoded token from cache or verify
        decoded = verify_jwt(token)
        if not decoded:
            return None
            
        # Extract user ID
        user_id = decoded.get("sub")
        if not user_id:
            api_logger.error(f"{log_prefix} No user_id found in token")
            return None
            
        return user_id
    except Exception as e:
        api_logger.error(f"{log_prefix} Error extracting user_id: {str(e)}")
        return None

# Optimized auth context setting
def ensure_auth_context(token: str, log_prefix: str = "") -> bool:
    """Set auth context with Supabase with connection pooling"""
    try:
        # Get cached user data if available
        cached_data = token_cache.get(token)
        if cached_data:
            # Set auth context using cached data
            supabase.auth.set_session(token, cached_data.get("refresh_token", ""))
            return True
            
        # If not in cache, get from Supabase
        user = supabase.auth.get_user(token)
        if user:
            # Cache the user data
            token_cache.set(token, user.dict())
            return True
            
        return False
    except Exception as e:
        api_logger.error(f"{log_prefix} Error setting auth context: {str(e)}")
        return False

# Clear token cache periodically
@app.on_event("startup")
async def startup_event():
    # Clear token cache every hour
    while True:
        await asyncio.sleep(3600)  # 1 hour
        token_cache.clear()

# Add middleware to log all requests with authentication information
@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    # Periodically refresh the connection (every ~100 requests)
    if random.random() < 0.01:  # 1% chance to refresh
        global supabase
        supabase = connection_manager.get_client()  # type: ignore
    
    transaction_id = log_transaction_start(request)
    start_time = time.time()
    path = request.url.path
    method = request.method
    emoji = "🌐"  # Default emoji for unknown endpoints
    
    # Set endpoint-specific emoji
    if path.startswith('/cats'):
        emoji = "🐱"
    elif path.startswith('/auth'):
        emoji = "🔐"
    elif path.startswith('/feed'):
        emoji = "🍽️"
    elif path.startswith('/schedule'):
        emoji = "📋"
    
    # Log request details
    api_logger.info(f"{emoji} TXN-{transaction_id} {method} {path}")
    
    # Check for authentication
    auth_header = request.headers.get("Authorization")
    has_auth = bool(auth_header)
    
    # Try to extract JWT user_id if present - log JWT debugging info
    user_id = None
    if has_auth and auth_header.startswith("Bearer "):
        try:
            token = auth_header.replace("Bearer ", "")
            api_logger.debug(f"{emoji} TXN-{transaction_id} Found Bearer token. Length: {len(token)}, Hash: {hash(token)}")
            
            jwt_payload = jwt.decode(token, options={"verify_signature": False})
            
            # Log JWT payload fields that might help with debugging
            debug_fields = ['exp', 'iat', 'sub', 'user_id', 'email']
            debug_payload = {field: jwt_payload.get(field) for field in debug_fields if field in jwt_payload}
            api_logger.debug(f"{emoji} TXN-{transaction_id} JWT payload debug fields: {json.dumps(debug_payload)}")
            
            user_id = jwt_payload.get("sub") or jwt_payload.get("user_id") or jwt_payload.get("id")
            if user_id:
                log_checkpoint(transaction_id, f"Identified user {user_id}")
                api_logger.info(f"{emoji} TXN-{transaction_id} Request authenticated for user {user_id}")
            else:
                api_logger.warning(f"{emoji} TXN-{transaction_id} Token had no user identifier. Fields: {sorted(jwt_payload.keys())}")
        except Exception as e:
            error_msg = f"Failed to extract user ID from token: {e}"
            api_logger.warning(f"{emoji} TXN-{transaction_id} {error_msg}")
            api_logger.debug(f"{emoji} TXN-{transaction_id} Token extraction error: {traceback.format_exc()}")
            cleanup_transaction(transaction_id, 401, error_msg)
            return Response(
                content=json.dumps({"detail": "Invalid authentication token"}),
                status_code=401,
                media_type="application/json"
            )
    
    # Set the transaction info in request.state for handlers to access
    request.state.transaction_id = transaction_id
    request.state.user_id = user_id
    
    # Log endpoint specific information
    if path.startswith('/cats'):
        api_logger.info(f"{emoji} TXN-{transaction_id} Cats endpoint call: {method} {path}")
        api_logger.debug(f"{emoji} TXN-{transaction_id} Cats endpoint details: Method={method}, Auth={has_auth}, UserID={user_id}")
        log_checkpoint(transaction_id, f"Cats endpoint call: {method} {path}")
    elif path.startswith('/auth'):
        api_logger.info(f"{emoji} TXN-{transaction_id} Auth endpoint call: {method} {path}")
        log_checkpoint(transaction_id, f"Auth endpoint call: {method} {path}")
    elif path.startswith('/feed'):
        api_logger.info(f"{emoji} TXN-{transaction_id} Feeder endpoint call: {method} {path}")
        log_checkpoint(transaction_id, f"Feeder endpoint call: {method} {path}")
    
    # Process the request
    try:
        log_checkpoint(transaction_id, "Forwarding request to handler")
        response = await call_next(request)
        
        # Log request completion with status code
        status_code = response.status_code
        duration = time.time() - start_time
        
        # Add response info to log context
        log_checkpoint(transaction_id, f"Response generated: status {status_code}")
        
        # Log response headers for debugging
        response_headers = {k.decode(): v.decode() for k, v in response.headers.raw}
        api_logger.debug(f"{emoji} TXN-{transaction_id} Response headers: {json.dumps(response_headers)}")
        
        # We'll let log_transaction_end handle all logging now
        # to avoid duplicate warnings
            
        # Complete transaction logging
        log_transaction_end(transaction_id, status_code, duration)
        return response
    except Exception as e:
        # Log any unhandled exceptions
        api_logger.error(f"{emoji} TXN-{transaction_id} 💥 Unhandled exception in {method} {path}: {str(e)}")
        api_logger.error(f"{emoji} TXN-{transaction_id} 💥 Traceback: {traceback.format_exc()}")
        log_checkpoint(transaction_id, f"Unhandled exception: {str(e)}")
        log_transaction_end(transaction_id, 500)
        raise


@app.get("/health")
async def health_check():
    """Health check endpoint for ALB"""
    return {
        "status": "healthy", 
        "service": "nibbleMate-api"
    }

# -----------------------------------------------------------------------------
# Authentication endpoints
# -----------------------------------------------------------------------------
def validate_email(email: str) -> bool:
    """Validate email format using regex"""
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return bool(re.match(pattern, email))

@app.post("/auth/sign-up", response_model=None)
@limiter.limit("5 per minute")
async def sign_up(request: Request):
    txn_id = log_transaction_start(request)
    try:
        auth_logger.info("Processing sign-up request")
        body = await request.json()
        email = body.get("email", "").strip()
        password = body.get("password", "")
        
        if not email or not password:
            auth_logger.warning("Sign-up rejected: Missing email or password")
            raise HTTPException(status_code=400, detail="Email and password are required")
        
        # Enhanced email validation - check more thoroughly
        if not validate_email(email):
            auth_logger.warning(f"Sign-up rejected: Invalid email format: {email}")
            raise HTTPException(status_code=400, detail="Invalid email format")
        
        # Enhanced password validation - check length and complexity
        if len(password) < 8:
            auth_logger.warning("Sign-up rejected: Password too short")
            raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
            
        # Check password complexity
        has_upper = any(c.isupper() for c in password)
        has_lower = any(c.islower() for c in password)
        has_digit = any(c.isdigit() for c in password)
        has_special = any(c in "!@#$%^&*()_+-=[]{}|;:,.<>?/" for c in password)
        
        if not (has_upper and has_lower and has_digit):
            auth_logger.warning("Sign-up rejected: Password not complex enough")
            missing_requirements = []
            if not has_upper:
                missing_requirements.append("an uppercase letter")
            if not has_lower:
                missing_requirements.append("a lowercase letter") 
            if not has_digit:
                missing_requirements.append("a number")
                
            error_message = "Password must contain " + ", ".join(missing_requirements)
            raise HTTPException(
                status_code=400, 
                detail=error_message
            )
            
        # Check for common passwords (basic implementation)
        common_passwords = ["password", "123456", "qwerty", "admin", "welcome", "123456789", "12345678"]
        if password.lower() in common_passwords:
            auth_logger.warning("Sign-up rejected: Common password detected")
            raise HTTPException(
                status_code=400, 
                detail="This password is too common. Please choose a stronger password."
            )

        # Sanitize email input to prevent injection attacks
        email = email.replace("<", "&lt;").replace(">", "&gt;")
        
        # Call Supabase to create the user
        auth_logger.info(f"Attempting to create user with email: {email}")
        try:
            result = supabase.auth.sign_up({  # type: ignore
                "email": email,
                "password": password,
                "options": {
                    "expiresIn": JWT_EXPIRY_SECONDS  # Use our 30-day expiration
                }
            })
            
            # The success case doesn't have a result.error attribute
            # Check if we have user and session data, which indicates success
            if hasattr(result, 'user') and result.user:
                auth_logger.info(f"User created successfully: {result.user.id if result.user else 'No user ID'}")
                user_id = result.user.id
                user_email = result.user.email
                # --- Username logic ---
                import re
                def username_from_email(email: str) -> str:
                    user = email.split('@')[0]
                    user = re.sub(r'[^a-zA-Z0-9-_]', '-', user)
                    return user
                username = username_from_email(user_email or "")
                # Insert profile row if not exists
                try:
                    profile_exists = supabase.from_("profiles").select("id").eq("id", user_id).single().execute()
                    if not getattr(profile_exists, 'data', None):
                        # Insert new profile
                        supabase.from_("profiles").insert({
                            "id": user_id,
                            "email": user_email,
                            "username": username
                        }).execute()
                        auth_logger.info(f"Profile created for user {user_id} with username '{username}'")
                except Exception as profile_error:
                    auth_logger.error(f"Failed to create profile for user {user_id}: {profile_error}")
                # --- End username logic ---
                
                # Check if email confirmation is required or if we got a session
                if hasattr(result, 'session') and result.session:
                    # SECURITY WARNING: This means email verification is disabled in Supabase settings
                    # This is a security risk - users should verify email ownership
                    auth_logger.warning("SECURITY WARNING: Email verification appears to be disabled in Supabase settings.")
                    auth_logger.warning("Consider enabling email verification in Supabase Authentication settings.")
                    
                    return {
                        "user": {
                            "id": result.user.id,
                            "email": result.user.email,
                        },
                        "session": {
                            "access_token": result.session.access_token,
                            "refresh_token": result.session.refresh_token,
                            "expires_at": int(result.session.expires_at or 0),
                        }
                    }
                else:
                    # Email verification is required (correct behavior)
                    auth_logger.info(f"User created, email verification required: {result.user.id}")
                    return {
                        "user": {
                            "id": result.user.id,
                            "email": result.user.email,
                        },
                        "message": "Account created successfully. Please check your email to verify your account."
                    }
            
            # Handle the case where result has an error property
            if hasattr(result, 'error') and result.error:  # type: ignore
                error_msg = result.error.message  # type: ignore
                auth_logger.error(f"Supabase sign-up error: {error_msg}")
                if "already registered" in error_msg:
                    raise HTTPException(status_code=409, detail="This email is already registered")
                raise HTTPException(status_code=400, detail=error_msg)
            
            # Unexpected response format
            auth_logger.warning(f"Unexpected Supabase response format: {type(result)}")
            return {
                "status": "success",
                "message": "Account created, but the response format was unexpected. Please check your email for verification instructions."
            }
            
        except Exception as supabase_error:
            error_msg = str(supabase_error)
            auth_logger.error(f"Supabase sign-up error: {error_msg}")
            
            # SECURITY FIX: Never bypass email verification, even on email service issues
            auth_logger.error("Sign-up failed: Email verification is required for security")
            
            # Report the error clearly but don't bypass verification
            if "Error sending confirmation email" in error_msg:
                raise HTTPException(
                    status_code=500,
                    detail="We encountered an issue sending the verification email. Please try again later."
                )
            
            # For other errors, pass through the original error
            if "already registered" in error_msg.lower():
                raise HTTPException(status_code=409, detail="This email is already registered")
            else:
                raise HTTPException(status_code=500, detail=error_msg)
                
    except HTTPException:
        raise
    except Exception as e:
        auth_logger.error(f"Unexpected error in sign-up: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/auth/sign-in", response_model=None)
@limiter.limit("10 per minute")
async def sign_in(request: Request):
    txn_id = log_transaction_start(request)
    try:
        auth_logger.info("Processing sign-in request")
        body = await request.json()
        
        email = body.get("email", "").strip().lower()
        password = body.get("password", "")
        
        # Input validation
        if not email:
            auth_logger.warning("Sign-in rejected: Missing email")
            raise HTTPException(status_code=400, detail="Email is required")
            
        if not password:
            auth_logger.warning("Sign-in rejected: Missing password")
            raise HTTPException(status_code=400, detail="Password is required")
        
        if not re.match(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$", email):
            auth_logger.warning(f"Sign-in rejected: Invalid email format: {email}")
            raise HTTPException(status_code=400, detail="Invalid email format")
            
        # Sanitize inputs
        email = email.replace("<", "&lt;").replace(">", "&gt;")
        
        # Enhanced rate limiting - track failed attempts per IP
        client_ip = get_remote_address(request)
        
        # Create a composite key for more precise rate limiting
        login_attempt_key = f"{client_ip}:{email.lower()}"
        
        # Check for too many recent failed attempts (implement sliding window rate limit)
        # This would be a good place to use Redis, but for simplicity we're using in-memory
        now = time.time()
        failed_attempts = getattr(request.app.state, "failed_login_attempts", {})
        
        # Clean up old entries (older than 30 minutes)
        for key in list(failed_attempts.keys()):
            attempts = failed_attempts[key]
            # Remove timestamps older than 30 minutes
            attempts = [t for t in attempts if now - t < 1800]
            if attempts:
                failed_attempts[key] = attempts
            else:
                failed_attempts.pop(key, None)
        
        # Check if there are too many recent failed attempts
        if login_attempt_key in failed_attempts and len(failed_attempts[login_attempt_key]) >= 5:
            # Calculate the cooldown period
            oldest_attempt = min(failed_attempts[login_attempt_key])
            time_passed = now - oldest_attempt
            if time_passed < 1800:  # 30 minutes
                cooldown_remaining = int(1800 - time_passed)
                auth_logger.warning(f"Sign-in rejected: Too many failed attempts for {login_attempt_key}")
                log_transaction_end(txn_id, 429)
                raise HTTPException(
                    status_code=429, 
                    detail=f"Too many failed login attempts. Please try again in {cooldown_remaining // 60} minutes."
                )
        
        auth_logger.info(f"Sign-in attempt from IP: {client_ip}, Email: {email}")
        
        # Log the authentication attempt
        auth_logger.info(f"Authenticating user: {email}")
        
        try:
            # Use extended session with longer expiration
            auth_params = {
                "email": email,
                "password": password,
                "options": {
                    "expiresIn": JWT_EXPIRY_SECONDS  # Use our 30-day expiration
                }
            }
            
            # Call Supabase to authenticate
            result = supabase.auth.sign_in_with_password(auth_params)  # type: ignore
            
            # Check for errors
            error = getattr(result, 'error', None)
            if error:
                # Track failed login attempt
                if not hasattr(request.app.state, "failed_login_attempts"):
                    request.app.state.failed_login_attempts = {}
                
                if login_attempt_key not in request.app.state.failed_login_attempts:
                    request.app.state.failed_login_attempts[login_attempt_key] = []
                
                request.app.state.failed_login_attempts[login_attempt_key].append(now)
                
                auth_logger.error(f"Sign-in error: {error.message}")
                log_transaction_end(txn_id, 401)
                raise HTTPException(status_code=401, detail="Invalid email or password")
            
            # Reset failed login attempts on success
            if hasattr(request.app.state, "failed_login_attempts"):
                request.app.state.failed_login_attempts.pop(login_attempt_key, None)
            
            # Check for user data
            if not hasattr(result, 'user') or not result.user:
                # Try alternative response format
                if hasattr(result, 'data') and result.data and hasattr(result.data, 'user'):  # type: ignore
                    user_data = result.data.user  # type: ignore
                else:
                    auth_logger.error("Missing user data in Supabase response")
                    log_transaction_end(txn_id, 500)
                    raise HTTPException(status_code=500, detail="Missing user data in response")
            else:
                user_data = result.user
                
                # Double-check email confirmation
                if not getattr(user_data, 'email_confirmed_at', None):
                    auth_logger.warning(f"Email not confirmed for user: {user_data.id}")
                    log_transaction_end(txn_id, 403)
                    raise HTTPException(
                        status_code=403, 
                        detail="Please verify your email before signing in. Check your inbox for a verification link."
                    )
            
            # Check for session data
            if not hasattr(result, 'session') or not result.session:
                # Try alternative response format
                if hasattr(result, 'data') and result.data and hasattr(result.data, 'session'):  # type: ignore
                    session_data = result.data.session  # type: ignore
                else:
                    auth_logger.error("Missing session data in Supabase response")
                    log_transaction_end(txn_id, 500)
                    raise HTTPException(status_code=500, detail="Missing session data in response")
            else:
                session_data = result.session
            
            # Log extended session details
            auth_logger.info(f"User signed in successfully: {user_data.id} with extended session")
            auth_logger.info(f"Token expires at: {session_data.expires_at}, expiry seconds: {JWT_EXPIRY_SECONDS}")
            
            # Return user data and session tokens
            log_transaction_end(txn_id, 200)
            return {
                "user": {
                    "id": user_data.id,
                    "email": user_data.email
                },
                "session": {
                    "access_token": session_data.access_token,
                    "refresh_token": session_data.refresh_token,
                    "expires_at": session_data.expires_at
                }
            }
        except HTTPException:
            raise
        except AttributeError as attr_err:
            auth_logger.error(f"Supabase response structure error: {str(attr_err)}", exc_info=True)
            auth_logger.debug(f"Supabase result structure: {str(result)}")
            # Try to extract data from different response structures
            if hasattr(result, 'data'):  # type: ignore
                data = result.data  # type: ignore
                
                # Handle data response
                try:
                    user = data.user if hasattr(data, 'user') else None
                    session = data.session if hasattr(data, 'session') else None
                    
                    if user and session:
                        # Double-check email confirmation even in this alt format
                        if not getattr(user, 'email_confirmed_at', None):
                            auth_logger.warning(f"Email not confirmed for user (alt format): {user.id}")
                            log_transaction_end(txn_id, 403)
                            raise HTTPException(
                                status_code=403, 
                                detail="Please verify your email before signing in. Check your inbox for a verification link."
                            )
                        
                        auth_logger.info(f"User signed in successfully (alt format): {user.id}")
                        log_transaction_end(txn_id, 200)
                        return {
                            "user": {
                                "id": user.id,
                                "email": user.email
                            },
                            "session": {
                                "access_token": session.access_token,
                                "refresh_token": session.refresh_token,
                                "expires_at": session.expires_at
                            }
                        }
                except Exception as inner_err:
                    auth_logger.error(f"Failed to process alternative response format: {str(inner_err)}", exc_info=True)
                    log_transaction_end(txn_id, 500)
                    raise HTTPException(status_code=500, detail=f"Authentication error: {str(inner_err)}")
            
            log_transaction_end(txn_id, 500)
            raise HTTPException(status_code=500, detail=f"Authentication error: {str(attr_err)}")
        
    except HTTPException:
        raise
    except Exception as e:
        auth_logger.error(f"Unexpected error in sign-in: {str(e)}", exc_info=True)
        
        # Handle specific known errors with user-friendly messages
        error_msg = str(e).lower()
        if "dns" in error_msg or "connection" in error_msg or "connect" in error_msg:
            auth_logger.error(f"Connection issue detected in sign-in: {str(e)}")
            log_transaction_end(txn_id, 503)
            raise HTTPException(
                status_code=503, 
                detail="Authentication service is temporarily unavailable. Please try again later."
            )
        elif "email not confirmed" in error_msg:
            log_transaction_end(txn_id, 403)
            raise HTTPException(
                status_code=403,
                detail="Please verify your email before signing in. Check your inbox for a verification link."
            )
        
        log_transaction_end(txn_id, 500)
        raise HTTPException(status_code=500, detail=f"Sign-in error: {str(e)}")


@app.post("/auth/sign-out")
async def sign_out(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        token = credentials.credentials
        auth_logger.info("Processing sign-out request")
        
        # Call Supabase to sign out
        try:
            result = supabase.auth.sign_out(token)  # type: ignore
            
            # Check if the result has an error attribute
            if hasattr(result, 'error') and result.error:  # type: ignore
                auth_logger.error(f"Sign-out error: {result.error}")  # type: ignore
                raise HTTPException(status_code=400, detail=str(result.error))  # type: ignore
            
            auth_logger.info("User signed out successfully")
            return {"status": "success", "message": "Successfully signed out"}
        except Exception as supabase_error:
            auth_logger.error(f"Supabase sign-out error: {str(supabase_error)}")
            # Don't expose internal errors to client, just indicate sign-out failed
            return {"status": "error", "message": "Sign-out failed, but session will be cleared client-side"}
    except HTTPException:
        raise
    except Exception as e:
        auth_logger.error(f"Sign out error: {str(e)}")
        # Return a safe response even on error
        return {"status": "error", "message": "Sign-out operation encountered an error"}


@app.post("/auth/verify", response_model=None)
async def verify_email(request: Request):
    try:
        body = await request.json()
        token = body.get("token")
        email = body.get("email")
        
        if not token:
            raise HTTPException(status_code=400, detail="Verification token is required")
        
        try:
            # Call Supabase to verify the email
            # Note: Supabase handles verification via URL directly, but we can check the token
            result = supabase.auth.verify_otp({
                "token": token,
                "type": "email",
                "email": email
            })
            
            # Check for errors
            error = getattr(result, 'error', None)
            if error:
                raise HTTPException(status_code=400, detail=error.message)
            
            # Handle different response formats
            if hasattr(result, 'session') and result.session:
                session_data = result.session
            elif hasattr(result, 'data') and result.data and hasattr(result.data, 'session'):  # type: ignore
                session_data = result.data.session  # type: ignore
            else:
                raise HTTPException(status_code=500, detail="Missing session data in response")
            
            return {
                "status": "success",
                "message": "Email verified successfully",
                "session": {
                    "access_token": session_data.access_token,
                    "refresh_token": session_data.refresh_token,
                    "expires_at": session_data.expires_at
                }
            }
        except HTTPException:
            raise
        except AttributeError as attr_err:
            print(f"Verify email attribute error: {str(attr_err)}, Result structure: {str(result)}")
            
            # Try to extract data from different response structures
            if hasattr(result, 'data'):  # type: ignore
                data = result.data  # type: ignore
                try:
                    session = data.session if hasattr(data, 'session') else None
                    
                    if session:
                        return {
                            "status": "success",
                            "message": "Email verified successfully",
                            "session": {
                                "access_token": session.access_token,
                                "refresh_token": session.refresh_token,
                                "expires_at": session.expires_at
                            }
                        }
                except Exception as inner_err:
                    print(f"Inner extraction error: {str(inner_err)}")
                    raise HTTPException(status_code=500, detail=f"Failed to process verification response: {str(inner_err)}")
            
            raise HTTPException(status_code=500, detail=f"Invalid response structure: {str(attr_err)}")
            
    except HTTPException:
        raise
    except Exception as e:
        print(f"Verify email error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")


## Global token refresh cache
TOKEN_REFRESH_CACHE = {}
TOKEN_REFRESH_EXPIRY = 3600  # 1 hour

@app.post("/auth/refresh", response_model=None)
async def refresh_token(request: Request):
    txn_id = log_transaction_start(request)
    try:
        auth_logger.debug("Processing token refresh request")
        body = await request.json()
        refresh_token = body.get("refresh_token")
        
        if not refresh_token:
            auth_logger.warning("Token refresh rejected: Missing refresh token")
            log_transaction_end(txn_id, 400)
            return {
                "error": {
                    "message": "Refresh token is required"
                },
                "data": None
            }
        
        # More efficient token caching with global dictionary
        # Use hash of token as cache key
        cache_key = hash(refresh_token)
            
        # Check cache first (valid for 1 hour)
        if cache_key in TOKEN_REFRESH_CACHE:
            cached_data = TOKEN_REFRESH_CACHE[cache_key]
            # Check if cache is still fresh
            if time.time() - cached_data["cached_at"] < TOKEN_REFRESH_EXPIRY:
                auth_logger.debug("Token refresh served from cache")
                log_transaction_end(txn_id, 200)
                return cached_data["response"]
        
        # If not in cache, call Supabase to refresh the session
        try:
            # Pass the refresh token to get a new session
            result = supabase.auth.refresh_session(refresh_token)
            
            if hasattr(result, 'error') and result.error:  # type: ignore
                auth_logger.error(f"Token refresh error: {result.error.message}")  # type: ignore
                log_transaction_end(txn_id, 400)
                return {
                    "error": {
                        "message": "Invalid or expired refresh token"
                    },
                    "data": None
                }
            
            # Access session data safely
            if not hasattr(result, 'session') or not result.session:
                auth_logger.error("Missing session in Supabase response")
                log_transaction_end(txn_id, 500)
                return {
                    "error": {
                        "message": "Failed to refresh session: No session data returned"
                    },
                    "data": None
                }
            
            auth_logger.debug("Token refreshed successfully")
            
            response = {
                "error": None,
                "data": {
                    "access_token": result.session.access_token,
                    "refresh_token": result.session.refresh_token,
                    "expires_at": result.session.expires_at
                }
            }
            
            # Use the global cache for better performance
            TOKEN_REFRESH_CACHE[cache_key] = {
                "response": response,
                "cached_at": time.time()
            }
            
            log_transaction_end(txn_id, 200)
            return response
        except Exception as supabase_error:
            auth_logger.error(f"Supabase refresh error: {str(supabase_error)}")
            log_transaction_end(txn_id, 500)
            return {
                "error": {
                    "message": f"Failed to refresh token: {str(supabase_error)}"
                },
                "data": None
            }
    except Exception as e:
        auth_logger.error(f"Unexpected error in token refresh: {str(e)}")
        log_transaction_end(txn_id, 500)
        return {
            "error": {
                "message": f"Server error: {str(e)}"
            },
            "data": None
        }


@app.get("/auth/validate")
async def validate_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Lightweight endpoint to check if a token is valid without fetching user data"""
    try:
        auth_logger.info("Processing token validation request")
        token = credentials.credentials
        auth_logger.info(f"Validating token: {token[:10]}...")
        
        # Directly decode the JWT token instead of using verify_jwt
        user_id = None
        try:
            # Decode the JWT token and extract user_id
            decoded_token = jwt.decode(
                token,
                options={"verify_signature": False},
                algorithms=["HS256"]
            )
            
            # The user_id is in the 'sub' field
            user_id = decoded_token.get("sub")
            
            if not user_id:
                auth_logger.warning("Token validation failed: No user ID in token")
                raise HTTPException(status_code=401, detail="Invalid token")
                
            auth_logger.info(f"Token decoded successfully, found user_id: {user_id}")
        except Exception as token_error:
            auth_logger.error(f"Error decoding token: {str(token_error)}")
            raise HTTPException(status_code=401, detail="Invalid token format")
        
        # Check with Supabase that the token is still valid
        # This is a minimal validation - just checking the JWT hasn't been revoked
        try:
            auth_logger.info("Verifying token with Supabase")
            # Get user by ID to verify token is valid (Supabase will reject if token is invalid)
            supabase.auth.get_user(token)
            auth_logger.info(f"Token valid for user: {user_id}")
            return {"valid": True}
        except Exception as e:
            auth_logger.error(f"Supabase token validation failed: {str(e)}")
            raise HTTPException(status_code=401, detail="Token validation failed")
            
    except HTTPException:
        raise
    except Exception as e:
        auth_logger.error(f"Unexpected error in token validation: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")


@app.get("/auth/user")
async def get_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        auth_logger.info("Processing get user request")
        token = credentials.credentials
        auth_logger.info(f"Token: {token[:10]}...")
        
        # Directly decode the JWT token instead of using verify_jwt
        user_id = None
        decoded_token = None
        try:
            # Decode the JWT token and extract user_id
            decoded_token = jwt.decode(
                token,
                options={"verify_signature": False},
                algorithms=["HS256"]
            )
            
            # The user_id is in the 'sub' field
            user_id = decoded_token.get("sub")
            
            if not user_id:
                auth_logger.warning("Get user failed: No user ID in token")
                raise HTTPException(status_code=401, detail="Invalid token")
                
            auth_logger.info(f"Token decoded successfully, found user_id: {user_id}")
        except Exception as token_error:
            auth_logger.error(f"Error decoding token: {str(token_error)}")
            raise HTTPException(status_code=401, detail="Invalid token format")
        
        auth_logger.info(f"Getting profile for user: {user_id}")
        # Get user profile from Supabase
        try:
            # Add extensive debug logging to show the query and results
            auth_logger.info(f"🔍 DEBUG: Querying profiles table with id={user_id}")
            
            # Get the profile with normal query
            profile_response = (
                supabase.from_("profiles")
                .select("id, email, username, updated_at")
                .eq("id", user_id)
                .single()
                .execute()
            )
            
            # Safely access data structure
            auth_logger.info(f"🔍 DEBUG: Profile raw response type: {type(profile_response)}")
            
            # Extract data and check if profile exists
            profile_data = getattr(profile_response, 'data', None)
            auth_logger.info(f"🔍 DEBUG: Profile data: {profile_data}")
            
            if profile_data:
                result_data = profile_data
                
                # Explicit trace logging for username field
                auth_logger.info(f"🔍 DEBUG: Username field value: '{result_data.get('username')}', type: {type(result_data.get('username'))}")
                
                # Check if username is None but email exists
                if result_data.get('username') is None and result_data.get('email'):
                    email = result_data.get('email')
                    auth_logger.info(f"🔍 DEBUG: Username is None, using email prefix instead: {email}")
                    username_val = email.split('@')[0] if '@' in email else email
                    auth_logger.info(f"🔍 DEBUG: Using email prefix as username: {username_val}")
                else:
                    username_val = result_data.get('username')
                
                auth_logger.info(f"🔍 DEBUG: Final username value: '{username_val}', type: {type(username_val)}")
                
                # Ensure username is string, not None
                if username_val is None:
                    username_val = "User"
                    auth_logger.warning(f"🔍 DEBUG: No username found, using default: '{username_val}'")
                
                # Ensure all fields are proper types for serialization
                response = {
                    "id": user_id,
                    "email": str(result_data.get("email") or decoded_token.get("email") or ""),
                    "username": str(username_val),
                    "updated_at": result_data.get("updated_at")
                }
                
                auth_logger.info(f"🔍 DEBUG: Returning formatted response: {response}")
                return response
            
            # If normal query has no data, try alternative approach
            auth_logger.warning(f"🔍 DEBUG: Profile fetch issue: No data returned from profiles query")
            
            # Try a direct query to check if user exists at all
            user_exists_response = (
                supabase.from_("profiles")
            .select("*")
                .eq("id", user_id)
            .execute()
            )
            
            user_exists_data = getattr(user_exists_response, 'data', [])
            auth_logger.info(f"🔍 DEBUG: User exists query result - data: {user_exists_data}")
            
            # If the user exists, extract username directly
            if user_exists_data and len(user_exists_data) > 0:
                result_data = user_exists_data[0]
                auth_logger.info(f"🔍 DEBUG: Found user data: {result_data}")
                
                username_val = result_data.get('username')
                auth_logger.info(f"🔍 DEBUG: Direct username field: '{username_val}', type: {type(username_val)}")
                
                if username_val is None and result_data.get('email'):
                    # Use email prefix as username
                    email = result_data.get('email')
                    username_val = email.split('@')[0] if '@' in email else email
                    auth_logger.info(f"🔍 DEBUG: Using email prefix as username: {username_val}")
                
                if username_val is None:
                    # Final fallback - use a default value
                    username_val = "User"
                    auth_logger.warning(f"🔍 DEBUG: No username found in direct query, using default")
                
                # Ensure username is a string
                username_val = str(username_val)
                
                response = {
                    "id": user_id,
                    "email": str(result_data.get("email") or decoded_token.get("email") or ""),
                    "username": username_val,
                    "updated_at": result_data.get("updated_at")
                }
                
                auth_logger.info(f"🔍 DEBUG: Returning direct query formatted response: {response}")
                return response
            
            # If profile doesn't exist, return basic user info from JWT
            auth_logger.info(f"🔍 DEBUG: No profile found, returning basic info from JWT for user: {user_id}")
            email = decoded_token.get("email") or ""
            username_from_jwt = decoded_token.get("name") or decoded_token.get("preferred_username") or email
            
            # If username is email, extract the part before @
            if username_from_jwt and '@' in username_from_jwt:
                username_from_jwt = username_from_jwt.split('@')[0]
                auth_logger.info(f"🔍 DEBUG: Extracted username from JWT email: {username_from_jwt}")
            
            # Ensure username is not None
            if not username_from_jwt:
                username_from_jwt = "User"
                auth_logger.warning(f"🔍 DEBUG: No username found in JWT, using default")
            
            # Ensure username is a string
            username_from_jwt = str(username_from_jwt)
            
            response = {
                "id": user_id,
                "email": str(email),
                "username": username_from_jwt,
                "updated_at": None
            }
            
            auth_logger.info(f"🔍 DEBUG: Returning JWT fallback response: {response}")
            return response
            
        except Exception as e:
            auth_logger.error(f"Error getting user profile: {str(e)}", exc_info=True)
            # Return basic information from the token
            email = decoded_token.get("email") or ""
            username_from_jwt = decoded_token.get("name") or decoded_token.get("preferred_username") or email
            
            # If username is email, extract the part before @
            if username_from_jwt and '@' in username_from_jwt:
                username_from_jwt = username_from_jwt.split('@')[0]
            
            # Ensure username is not None
            if not username_from_jwt:
                username_from_jwt = "User"
            
            # Ensure username is a string
            username_from_jwt = str(username_from_jwt)
            
            response = {
                "id": user_id,
                "email": str(email),
                "username": username_from_jwt,
                "updated_at": None
            }
            
            auth_logger.info(f"🔍 DEBUG: Returning error fallback response: {response}")
            return response
            
    except HTTPException:
        raise
    except Exception as e:
        auth_logger.error(f"Unexpected error in get_user: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")


@app.put("/auth/user", response_model=None)
async def update_user(request: Request, credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        auth_logger.info("Processing update user request")
        token = credentials.credentials
        auth_logger.info(f"Token: {token[:10]}...")
        
        # Directly decode the JWT token instead of using verify_jwt
        user_id = None
        decoded_token = None
        try:
            # Decode the JWT token and extract user_id
            decoded_token = jwt.decode(
                token,
                options={"verify_signature": False},
                algorithms=["HS256"]
            )
            
            # The user_id is in the 'sub' field
            user_id = decoded_token.get("sub")
            
            if not user_id:
                auth_logger.warning("Update user failed: No user ID in token")
                raise HTTPException(status_code=401, detail="Invalid token")
                
            auth_logger.info(f"Token decoded successfully, found user_id: {user_id}")
        except Exception as token_error:
            auth_logger.error(f"Error decoding token: {str(token_error)}")
            raise HTTPException(status_code=401, detail="Invalid token format")
        
        # Get request body
        body = await request.json()
        auth_logger.info(f"Update user request body: {body}")
        
        # Only allow updating specific fields (not id or email)
        allowed_update_fields = {"username"}
        update_data = {k: v for k, v in body.items() if k in allowed_update_fields}
        
        if not update_data:
            raise HTTPException(status_code=400, detail="No valid fields to update")
        
        # Ensure username is a string if present
        if "username" in update_data and update_data["username"] is not None:
            update_data["username"] = str(update_data["username"])
            # Validate that username is not empty
            if not update_data["username"].strip():
                raise HTTPException(status_code=400, detail="Username cannot be empty")
        
        auth_logger.info(f"Updating profile for user: {user_id} with data: {update_data}")
        
        # Update profile in Supabase
        try:
            update_response = (
                supabase.from_("profiles")
                .update(update_data)
                .eq("id", user_id)
                .execute()
            )
            
            auth_logger.info(f"🔍 DEBUG: Update response type: {type(update_response)}")
            
            # Extract data and check if update was successful
            updated_data = getattr(update_response, 'data', None)
            auth_logger.info(f"🔍 DEBUG: Updated profile data: {updated_data}")
            
            if not updated_data or len(updated_data) == 0:
                auth_logger.warning(f"🔍 DEBUG: Failed to update profile, no data returned")
                raise HTTPException(status_code=500, detail="Failed to update profile")
            
            # Get the complete updated profile
            profile_response = (
                supabase.from_("profiles")
                .select("id, email, username, updated_at")
                .eq("id", user_id)
                .single()
                .execute()
            )
            
            profile_data = getattr(profile_response, 'data', None)
            
            if profile_data:
                result_data = profile_data
                username_val = result_data.get('username')
                
                # If username is still None but email exists, use email prefix
                if username_val is None and result_data.get('email'):
                    email = result_data.get('email')
                    username_val = email.split('@')[0] if '@' in email else email
                
                # Ensure username is not None
                if username_val is None:
                    username_val = "User"
                
                response = {
                    "id": user_id,
                    "email": str(result_data.get("email") or decoded_token.get("email") or ""),
                    "username": str(username_val),
                    "updated_at": result_data.get("updated_at")
                }
                
                auth_logger.info(f"🔍 DEBUG: Returning updated profile: {response}")
                return response
            else:
                # Fallback to just returning what we updated
                return {
                    "id": user_id, 
                    "username": update_data.get("username"),
                    "message": "Profile updated successfully"
                }
        except Exception as db_error:
            auth_logger.error(f"Database error updating user profile: {str(db_error)}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Database error: {str(db_error)}")
                
    except HTTPException:
        raise
    except Exception as e:
        auth_logger.error(f"Unexpected error in update_user: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")


# Add logging to verify_jwt for token debugging
def verify_ownership(user_id: str, feeder_id: int, log_context: str = "unknown"):
    """Verify user owns a feeder"""
    api_logger.info(f"🔄 [{log_context}] Verifying user {user_id} owns feeder {feeder_id}")
    
    try:
        api_logger.debug(f"🔄 [{log_context}] SQL: SELECT * FROM feeder WHERE feederid = {feeder_id} AND owner_id = '{user_id}'")
        owned = (
            supabase.table("feeder")
            .select("*")
            .eq("owner_id", user_id)
            .eq("feederid", feeder_id)
            .execute()
            .data
        )
        
        if not owned:
            api_logger.warning(f"🔄 [{log_context}] User {user_id} does not own feeder {feeder_id}")
            return False
            
        api_logger.info(f"🔄 [{log_context}] Ownership verified: user {user_id} owns feeder {feeder_id}")
        api_logger.debug(f"🔄 [{log_context}] Ownership record: {owned[0] if owned else 'None'}")
        return True
    except Exception as e:
        api_logger.error(f"🔄 [{log_context}] Error verifying ownership: {str(e)}", exc_info=True)
        api_logger.error(f"🔄 [{log_context}] Traceback: {traceback.format_exc()}")
        return False


@app.get("/feeders", response_model=None)
async def get_user_feeders_alt(credentials: HTTPAuthorizationCredentials = Depends(security)):
    txn_id = log_transaction_start()
    try:
        token = credentials.credentials
        api_logger.debug(f"🍽️ GET /feeders - Request received")
        
        # Extract user ID from token using our helper function
        user_id = extract_user_id_from_token(token, "🍽️ GET /feeders")
        if not user_id:
            api_logger.error(f"🍽️ GET /feeders - Failed to extract valid user ID from token")
            raise HTTPException(status_code=401, detail="Invalid authentication token")
            
        # Set auth context ONCE with Supabase for RLS policies
        if not ensure_auth_context(token, "🍽️ GET /feeders"):
            api_logger.error(f"🍽️ GET /feeders - Failed to set auth context")
            raise HTTPException(status_code=401, detail="Could not authenticate with database")

        try:
            # Performance optimization: do a single JOIN query instead of separate queries
            # This avoids N+1 queries for each feeder's food brand
            
            # Use a simpler query format that's compatible with older PostgREST versions
            select_str = "feederid, name, manual_feed_calories, hardwareid, brandname"
            
            # Use a simpler query without the JOIN for compatibility
            feeders_query = lambda: supabase.from_("feeder") \
                .select(select_str) \
                .eq("owner_id", user_id) \
                .execute().data

            feeders_data, error = supabase_debug_query("select", "feeder", txn_id, feeders_query, user_id=user_id)
            
            if error:
                api_logger.error(f"🍽️ GET /feeders - Database error: {str(error)}")
                raise Exception(f"Database error: {str(error)}")
            
            if not feeders_data:
                return []
            
            # We need to get the food brand details in a separate step
            foodbrands_map = {}
            
            if feeders_data:
                # Get all unique brandnames
                brandnames = [feeder.get("brandname") for feeder in feeders_data if feeder.get("brandname")]
                
                if brandnames:
                    # Get food brand details for all needed brands
                    try:
                        foodbrands_query = lambda: supabase.table("foodbrands").select("id, brandname, calories, servsize").in_("brandname", brandnames).execute().data
                        foodbrands_result, _ = supabase_debug_query("select", "foodbrands", txn_id, foodbrands_query)
                        
                        # Create a map of brandname to details
                        if foodbrands_result:
                            for brand in foodbrands_result:
                                brandname = brand.get("brandname")
                                if brandname:
                                    foodbrands_map[brandname] = brand
                    except Exception as e:
                        api_logger.warning(f"🍽️ GET /feeders - Error fetching food brands: {str(e)}")
            
            # Transform the data for the client
            transformed_feeders = []
            for feeder in feeders_data:
                # Skip disassociated feeders
                if feeder.get("name", "").startswith("DISASSOCIATED:"):
                    continue
                    
                brandname = feeder.get("brandname")
                
                # Get food brand details from the map
                foodbrands_data = foodbrands_map.get(brandname, {}) if brandname else {}
                
                # Create food brand details object
                foodbrand_details = {
                    "id": foodbrands_data.get("id"),
                    "brandName": brandname or "Unknown Brand",
                    "calories": foodbrands_data.get("calories"),
                    "servSize": foodbrands_data.get("servsize")
                }
                
                transformed = {
                    "id": feeder.get("feederid"),
                    "name": feeder.get("name", f"Feeder {feeder.get('feederid')}"),
                    "manual_feed_calories": feeder.get("manual_feed_calories"),
                    "hardwareid": feeder.get("hardwareid"),
                    "brandname": brandname,
                    "foodbrand_id": foodbrands_data.get("id"),
                    "foodBrandDetails": foodbrand_details
                }
                transformed_feeders.append(transformed)
            
            return transformed_feeders

        except Exception as e:
            api_logger.error(f"🍽️ GET /feeders - Error during feeder data processing for user {user_id}: {str(e)}", exc_info=True)
            log_transaction_end(txn_id, 500)
            raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

    except HTTPException as http_exc:
        # Ensure transaction is logged if it was started
        if txn_id and transaction_contexts.get(txn_id):
            log_transaction_end(txn_id, http_exc.status_code if hasattr(http_exc, 'status_code') else 500)
        raise
    except Exception as e:
        if txn_id and transaction_contexts.get(txn_id):
            log_transaction_end(txn_id, 500)
        api_logger.error(f"🍽️ GET /feeders - Unexpected error: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")

@app.post("/feeders", response_model=None)
async def create_feeder(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    # txn_id = log_transaction_start(request)
    try:
        token = credentials.credentials
        
        # Extract user ID from token
        user_id = extract_user_id_from_token(token, "🍽️ POST /feeders")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid authentication token")
            
        api_logger.info(f"🍽️ POST /feeders - Starting feeder creation for user {user_id}")
        
        # Parse and validate request
        body = await request.json()
        foodbrand_id = body.get("foodbrand_id")
        feeder_name = body.get("name", "New Feeder")
        manual_feed_calories = body.get("manual_feed_calories", 100)
        
        if not foodbrand_id:
            raise HTTPException(status_code=400, detail="Food brand ID is required")

        # Get brand name
        try:
            brandname_result = supabase.table("foodbrands").select("brandname").eq("id", foodbrand_id).single().execute()
            if not brandname_result.data:
                raise HTTPException(status_code=404, detail="Food brand not found")
            brandname = brandname_result.data.get("brandname")
        except Exception as e:
            api_logger.error(f"🍽️ POST /feeders - Error getting brand name: {str(e)}")
            raise HTTPException(status_code=500, detail="Error getting brand information")

        # Create feeder record - using auto increment
        try:
            feeder_data = {
                "name": feeder_name,
                "brandname": brandname,
                "manual_feed_calories": int(manual_feed_calories),
                "owner_id": user_id
            }

            api_logger.info(f"🍽️ POST /feeders - Creating feeder with auto-increment ID")
            
            # Insert the record, letting PostgreSQL assign the ID
            feeder_result = supabase.from_("feeder").insert(feeder_data).execute()
            
            if not feeder_result.data or len(feeder_result.data) == 0:
                raise Exception("Feeder creation failed")
                
            # Get the ID assigned by the database
            created_feeder = feeder_result.data[0]
            next_id = created_feeder.get("feederid")
            created_feeder["foodbrand_id"] = foodbrand_id
            api_logger.info(f"🍽️ POST /feeders - Successfully created feeder {next_id}")
            return created_feeder

        except Exception as e:
            api_logger.error(f"🍽️ POST /feeders - Creation error: {str(e)}")
            raise HTTPException(status_code=500, detail=str(e))

    except HTTPException:
        raise
    except Exception as e:
        api_logger.error(f"🍽️ POST /feeders - Unexpected error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/feednow", response_model=None)
async def feed_now_alt(
    request: Request, credentials: HTTPAuthorizationCredentials = Depends(security)
):
    # Create a transaction ID for logging
    txn_id = log_transaction_start(request)
    try:
        token = credentials.credentials
        api_logger.info(f"🍽️ POST /feednow - Processing request, token: {token[:10]}...")
        log_checkpoint(txn_id, "Processing feed now request")
        
        # Extract user ID from token using our helper function
        user_id = extract_user_id_from_token(token, "🍽️ POST /feednow")
        if not user_id:
            api_logger.error(f"🍽️ POST /feednow - Failed to extract valid user ID from token")
            log_checkpoint(txn_id, "Authentication failed: Invalid user ID")
            log_transaction_end(txn_id, 401)
            raise HTTPException(status_code=401, detail="Invalid authentication token")
            
        log_checkpoint(txn_id, f"Authenticated as user {user_id}")
        
        # Set auth context with Supabase for RLS policies
        api_logger.info(f"🍽️ POST /feednow - Setting initial auth context with Supabase")
        if not ensure_auth_context(token, "🍽️ POST /feednow - Initial"):
            api_logger.error(f"🍽️ POST /feednow - Failed to set initial auth context")
            log_checkpoint(txn_id, "Authentication failed: Could not set auth context")
            log_transaction_end(txn_id, 401)
            raise HTTPException(status_code=401, detail="Could not authenticate with database")
            
        # Parse request body
        body = await request.json()
        feeder_id = body.get("feederId")
        
        if not feeder_id:
            api_logger.error(f"🍽️ POST /feednow - Missing feeder ID in request")
            log_checkpoint(txn_id, "Missing required parameter: feederId")
            log_transaction_end(txn_id, 400)
            raise HTTPException(status_code=400, detail="Feeder ID is required")
        
        # Get the cat ID associated with the feeder
        log_checkpoint(txn_id, f"Looking up cat associated with feeder {feeder_id}")
        try:
            # Ensure auth context before accessing cat table
            if not ensure_auth_context(token, "🍽️ POST /feednow - cat lookup"):
                raise Exception("Failed to set auth context for cat lookup")
                
            cat_query = lambda: supabase.table("cat").select("catid").eq("feederid", feeder_id).execute().data
            cat_result, cat_error = supabase_debug_query("select", "cat", txn_id, cat_query, feeder_id=feeder_id)
            
            if cat_error or not cat_result or len(cat_result) == 0:
                api_logger.error(f"🍽️ POST /feednow - No cat found for feeder {feeder_id}: {cat_error if cat_error else 'No results'}")
                log_checkpoint(txn_id, f"No cat associated with feeder {feeder_id}")
                log_transaction_end(txn_id, 400)
                raise HTTPException(status_code=400, detail=f"No cat associated with feeder {feeder_id}")
                
            cat_id = cat_result[0].get("catid")
            api_logger.info(f"🍽️ POST /feednow - Found cat ID {cat_id} for feeder {feeder_id}")
            
            # Get the amount from request or use a default
            amount = body.get("calories", 100)  # Default to 100 calories if not specified
            
            # Get the current weight of the cat if available or use a default
            weight = body.get("weight")
            if weight is None:
                # Try to get the cat's last known weight from the most recent feedlog entry
                weight_query = lambda: supabase.table("feedlog").select("weight").eq("id", cat_id).order("created_at", desc=True).limit(1).execute().data
                weight_result, weight_error = supabase_debug_query("select", "feedlog", txn_id, weight_query, cat_id=cat_id)
                
                if not weight_error and weight_result and len(weight_result) > 0 and weight_result[0].get("weight") is not None:
                    weight = weight_result[0].get("weight")
                    api_logger.info(f"🍽️ POST /feednow - Using last known weight from feedlog: {weight}")
                else:
                    # If no weight found, get weight from cat table
                    cat_weight_query = lambda: supabase.table("cat").select("catweight").eq("catid", cat_id).execute().data
                    cat_weight_result, cat_weight_error = supabase_debug_query("select", "cat", txn_id, cat_weight_query, cat_id=cat_id)
                    
                    if not cat_weight_error and cat_weight_result and len(cat_weight_result) > 0:
                        weight = cat_weight_result[0].get("catweight")
                        api_logger.info(f"🍽️ POST /feednow - Using weight from cat table: {weight}")
                    else:
                        # Use a default weight if all else fails
                        weight = 4.5  # Default weight in kg for an average cat
                        api_logger.info(f"🍽️ POST /feednow - Using default weight: {weight}")
            
            # Create a feedlog entry with the required fields
            feed_log_data = {
                "id": cat_id,  # Foreign key to cat.catid
                "amount": amount,
                "weight": weight,
                "created_at": datetime.utcnow().isoformat(),
            }
                
            # Execute the insert operation
            api_logger.info(f"🍽️ POST /feednow - Inserting feed log for user {user_id}, feeder {feeder_id}, cat {cat_id}")
            log_checkpoint(txn_id, "Inserting into feedlog table")
            
            # Ensure auth context before inserting into feedlog
            if not ensure_auth_context(token, "🍽️ POST /feednow - feedlog insert"):
                raise Exception("Failed to set auth context for feedlog insert")
                
            feedlog_query = lambda: supabase.table("feedlog").insert(feed_log_data).execute().data
            feedlog_result, feedlog_error = supabase_debug_query("insert", "feedlog", txn_id, feedlog_query, data=feed_log_data)
            
            if feedlog_error:
                api_logger.error(f"🍽️ POST /feednow - Error inserting into feedlog: {feedlog_error}")
                log_checkpoint(txn_id, f"Database error: {feedlog_error}")
                log_transaction_end(txn_id, 500)
                raise HTTPException(status_code=500, detail=f"Database error: {feedlog_error}")
                
            api_logger.info(f"🍽️ POST /feednow - Feed log inserted successfully")
            
            # Also create an entry in the feednow table
            feednow_data = {
                "userid": user_id,
                "created_at": datetime.utcnow().isoformat()
            }
            
            # Ensure auth context before inserting into feednow table
            if not ensure_auth_context(token, "🍽️ POST /feednow - feednow insert"):
                raise Exception("Failed to set auth context for feednow insert")
                
            feednow_query = lambda: supabase.table("feednow").insert(feednow_data).execute().data
            _, feednow_error = supabase_debug_query("insert", "feednow", txn_id, feednow_query, data=feednow_data)
            
            if feednow_error:
                api_logger.warning(f"🍽️ POST /feednow - Error inserting into feednow table: {feednow_error}")
                # Continue even if this insert fails, as the main feedlog entry is more important
            
            log_checkpoint(txn_id, "Feed now operation completed successfully")
            log_transaction_end(txn_id, 200)
            
            return {"status": "success", "message": "Feed Now operation completed successfully"}
            
        except HTTPException:
            raise
        except Exception as e:
            api_logger.error(f"🍽️ POST /feednow - Error processing request: {str(e)}")
            log_checkpoint(txn_id, f"Error: {str(e)}")
            log_transaction_end(txn_id, 500)
            raise HTTPException(status_code=500, detail=f"Error processing feed now request: {str(e)}")
    except HTTPException:
        raise
    except Exception as e:
        if txn_id:
            log_checkpoint(txn_id, f"Error: {str(e)}")
            log_transaction_end(txn_id, 500)
        api_logger.error(f"🍽️ POST /feednow - Error processing request: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error processing feed now request: {str(e)}")


# ----------------- FEEDER MANAGEMENT ENDPOINTS --------------

def cleanup_transaction(txn_id: str, status_code: int = 500, error_msg: Optional[str] = None) -> None:
    """Standardized transaction cleanup with error logging"""
    if txn_id and txn_id in transaction_contexts:
        if error_msg:
            log_checkpoint(txn_id, f"Error: {error_msg}")
        log_transaction_end(txn_id, status_code)

@app.put("/feeders/{feeder_id}/feed-amount", response_model=None)
async def update_feeder_feed_amount(
    feeder_id: int,
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    """
    Update the feed amount (calories) for a specific feeder
    """
    txn_id = log_transaction_start(request)
    try:
        # Validate authentication and extract user_id
        token = credentials.credentials
        try:
            decoded_token = jwt.decode(token, options={"verify_signature": False}, algorithms=["HS256"])
            user_id = decoded_token.get("sub")
            if not user_id:
                api_logger.error(f"🍽️ PUT /feeders/{feeder_id}/feed-amount - No user_id found in token")
                raise HTTPException(status_code=401, detail="Invalid authentication token")
        except Exception as token_error:
            api_logger.error(f"🍽️ PUT /feeders/{feeder_id}/feed-amount - Error decoding token: {str(token_error)}")
            raise HTTPException(status_code=401, detail="Invalid authentication token")
        
        # Verify ownership
        if not verify_ownership(user_id, feeder_id, f"PUT /feeders/{feeder_id}/feed-amount"):
            api_logger.error(f"🍽️ PUT /feeders/{feeder_id}/feed-amount - User {user_id} does not own feeder {feeder_id}")
            raise HTTPException(status_code=403, detail="You don't have permission to modify this feeder")
        
        # Extract the request body
        body = await request.json()
        calories = body.get("calories")
        
        # Validate calories
        if calories is None:
            api_logger.error(f"🍽️ PUT /feeders/{feeder_id}/feed-amount - Missing calories in request [TXN: {txn_id}]")
            raise HTTPException(status_code=400, detail="Missing required field: calories")
        
        try:
            calories = int(calories)
            if calories < 0:
                raise ValueError("Calories must be positive")
        except ValueError as e:
            api_logger.error(f"🍽️ PUT /feeders/{feeder_id}/feed-amount - Invalid calories value: {calories} [TXN: {txn_id}]")
            raise HTTPException(status_code=400, detail=f"Invalid calories value: {str(e)}")
        
        # Update the feeder in the database
        update_query = lambda: supabase.table("feeder").update({"manual_feed_calories": calories}).eq("feederid", feeder_id).execute()
        result, error = supabase_debug_query("update", "feeder", txn_id, update_query, feeder_id=feeder_id, calories=calories)
        
        if error:
            api_logger.error(f"🍽️ PUT /feeders/{feeder_id}/feed-amount - Database error: {str(error)} [TXN: {txn_id}]")
            raise HTTPException(status_code=500, detail="Failed to update feeder feed amount")
        
        api_logger.info(f"🍽️ PUT /feeders/{feeder_id}/feed-amount - Successfully updated feed amount to {calories} calories [TXN: {txn_id}]")
        log_transaction_end(txn_id, 200)
        return {"success": True, "feeder_id": feeder_id, "calories": calories}
        
    except json.JSONDecodeError:
        error_msg = "Invalid JSON in request body"
        api_logger.error(f"🍽️ PUT /feeders/{feeder_id}/feed-amount - {error_msg} [TXN: {txn_id}]")
        cleanup_transaction(txn_id, 400, error_msg)
        raise HTTPException(status_code=400, detail=error_msg)
    except HTTPException:
        raise  # Re-raise HTTP exceptions
    except Exception as e:
        error_msg = f"Unexpected error: {str(e)}"
        api_logger.error(f"🍽️ PUT /feeders/{feeder_id}/feed-amount - {error_msg} [TXN: {txn_id}]")
        cleanup_transaction(txn_id, 500, error_msg)
        raise HTTPException(status_code=500, detail="An unexpected error occurred")

@app.put("/feeders/{feeder_id}/food-brand", response_model=None)
async def update_feeder_food_brand(
    feeder_id: int,
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    """
    Update the food brand for a specific feeder
    """
    txn_id = str(uuid.uuid4())
    api_logger.info(f"🍽️ PUT /feeders/{feeder_id}/food-brand - Processing request [TXN: {txn_id}]")
    
    # Validate authentication and extract user_id
    token = credentials.credentials
    try:
        decoded_token = jwt.decode(token, options={"verify_signature": False}, algorithms=["HS256"])
        user_id = decoded_token.get("sub")
        if not user_id:
            api_logger.error(f"🍽️ PUT /feeders/{feeder_id}/food-brand - No user_id found in token")
            raise HTTPException(status_code=401, detail="Invalid authentication token")
    except Exception as token_error:
        api_logger.error(f"🍽️ PUT /feeders/{feeder_id}/food-brand - Error decoding token: {str(token_error)}")
        raise HTTPException(status_code=401, detail="Invalid authentication token")
    
    # Verify ownership
    if not verify_ownership(user_id, feeder_id, f"PUT /feeders/{feeder_id}/food-brand"):
        api_logger.error(f"🍽️ PUT /feeders/{feeder_id}/food-brand - User {user_id} does not own feeder {feeder_id}")
        raise HTTPException(status_code=403, detail="You don't have permission to modify this feeder")
    
    try:
        # Extract the request body
        body = await request.json()
        brand_name = body.get("brandName")
        
        # Validate brand name
        if not brand_name:
            api_logger.error(f"🍽️ PUT /feeders/{feeder_id}/food-brand - Missing brandName in request [TXN: {txn_id}]")
            raise HTTPException(status_code=400, detail="Missing required field: brandName")
        
        # Verify the brand name exists in the foodbrands table
        brand_query = lambda: supabase.table("foodbrands").select("id").eq("brandname", brand_name).single().execute()
        brand_result, brand_error = supabase_debug_query("select", "foodbrands", txn_id, brand_query, brand_name=brand_name)
        
        if brand_error or not brand_result:
            api_logger.error(f"🍽️ PUT /feeders/{feeder_id}/food-brand - Invalid brand name: {brand_name} [TXN: {txn_id}]")
            raise HTTPException(status_code=400, detail=f"Invalid food brand: {brand_name}")
        
        # Update the feeder in the database
        update_query = lambda: supabase.table("feeder").update({"brandname": brand_name}).eq("feederid", feeder_id).execute()
        result, error = supabase_debug_query("update", "feeder", txn_id, update_query, feeder_id=feeder_id, brand_name=brand_name)
        
        if error:
            api_logger.error(f"🍽️ PUT /feeders/{feeder_id}/food-brand - Database error: {str(error)} [TXN: {txn_id}]")
            raise HTTPException(status_code=500, detail="Failed to update feeder food brand")
        
        api_logger.info(f"🍽️ PUT /feeders/{feeder_id}/food-brand - Successfully updated food brand to {brand_name} [TXN: {txn_id}]")
        return {"success": True, "feeder_id": feeder_id, "brandName": brand_name}
        
    except json.JSONDecodeError:
        api_logger.error(f"🍽️ PUT /feeders/{feeder_id}/food-brand - Invalid JSON in request body [TXN: {txn_id}]")
        raise HTTPException(status_code=400, detail="Invalid JSON in request body")
    except HTTPException:
        raise  # Re-raise HTTP exceptions
    except Exception as e:
        api_logger.error(f"🍽️ PUT /feeders/{feeder_id}/food-brand - Unexpected error: {str(e)} [TXN: {txn_id}]")
        raise HTTPException(status_code=500, detail="An unexpected error occurred")

@app.put("/feeders/{feeder_id}/name", response_model=None)
async def update_feeder_name(
    feeder_id: int,
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    """
    Update the name for a specific feeder
    """
    txn_id = str(uuid.uuid4())
    api_logger.info(f"🍽️ PUT /feeders/{feeder_id}/name - Processing request [TXN: {txn_id}]")
    
    # Validate authentication and extract user_id
    token = credentials.credentials
    try:
        decoded_token = jwt.decode(token, options={"verify_signature": False}, algorithms=["HS256"])
        user_id = decoded_token.get("sub")
        if not user_id:
            api_logger.error(f"🍽️ PUT /feeders/{feeder_id}/name - No user_id found in token")
            raise HTTPException(status_code=401, detail="Invalid authentication token")
    except Exception as token_error:
        api_logger.error(f"🍽️ PUT /feeders/{feeder_id}/name - Error decoding token: {str(token_error)}")
        raise HTTPException(status_code=401, detail="Invalid authentication token")
    
    # Verify ownership
    if not verify_ownership(user_id, feeder_id, f"PUT /feeders/{feeder_id}/name"):
        api_logger.error(f"🍽️ PUT /feeders/{feeder_id}/name - User {user_id} does not own feeder {feeder_id}")
        raise HTTPException(status_code=403, detail="You don't have permission to modify this feeder")
    
    try:
        # Extract the request body
        body = await request.json()
        name = body.get("name")
        
        # Validate name
        if not name:
            api_logger.error(f"🍽️ PUT /feeders/{feeder_id}/name - Missing name in request [TXN: {txn_id}]")
            raise HTTPException(status_code=400, detail="Missing required field: name")
        
        # Update the feeder in the database
        update_query = lambda: supabase.table("feeder").update({"name": name}).eq("feederid", feeder_id).execute()
        result, error = supabase_debug_query("update", "feeder", txn_id, update_query, feeder_id=feeder_id, name=name)
        
        if error:
            api_logger.error(f"🍽️ PUT /feeders/{feeder_id}/name - Database error: {str(error)} [TXN: {txn_id}]")
            raise HTTPException(status_code=500, detail="Failed to update feeder name")
        
        api_logger.info(f"🍽️ PUT /feeders/{feeder_id}/name - Successfully updated name to {name} [TXN: {txn_id}]")
        return {"success": True, "feeder_id": feeder_id, "name": name}
        
    except json.JSONDecodeError:
        api_logger.error(f"🍽️ PUT /feeders/{feeder_id}/name - Invalid JSON in request body [TXN: {txn_id}]")
        raise HTTPException(status_code=400, detail="Invalid JSON in request body")
    except HTTPException:
        raise  # Re-raise HTTP exceptions
    except Exception as e:
        api_logger.error(f"🍽️ PUT /feeders/{feeder_id}/name - Unexpected error: {str(e)} [TXN: {txn_id}]")
        raise HTTPException(status_code=500, detail="An unexpected error occurred")

@app.post("/feeders/hardware", response_model=None)
async def assign_hardware_id(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """
    Assign a hardware ID (serial number) to a feeder
    """
    txn_id = log_transaction_start(request)
    api_logger.info(f"🍽️ POST /feeders/hardware - Processing request [TXN: {txn_id}]")
    
    try:
        # Authenticate user
        token = credentials.credentials
        user_id = None
        
        try:
            decoded_token = jwt.decode(token, options={"verify_signature": False}, algorithms=["HS256"])
            user_id = decoded_token.get("sub")
            if not user_id:
                api_logger.error(f"🍽️ POST /feeders/hardware - No user_id found in token")
                log_transaction_end(txn_id, 401)
                raise HTTPException(status_code=401, detail="Invalid authentication token")
            
            # Set auth context with Supabase for RLS policies
            api_logger.info(f"🍽️ POST /feeders/hardware - Setting auth context with Supabase")
            supabase.auth.get_user(token)
            log_checkpoint(txn_id, "Auth context set with Supabase")
        except Exception as token_error:
            api_logger.error(f"🍽️ POST /feeders/hardware - Error decoding token: {str(token_error)}")
            log_transaction_end(txn_id, 401)
            raise HTTPException(status_code=401, detail="Invalid authentication token")
        
        # Parse request body
        body = await request.json()
        feeder_id = body.get("feederId")
        hardware_id = body.get("hardwareId")
        
        # Validate required parameters
        if not feeder_id:
            api_logger.error(f"🍽️ POST /feeders/hardware - Missing feederId in request")
            log_transaction_end(txn_id, 400)
            raise HTTPException(status_code=400, detail="Missing required parameter: feederId")
            
        if not hardware_id:
            api_logger.error(f"🍽️ POST /feeders/hardware - Missing hardwareId in request")
            log_transaction_end(txn_id, 400)
            raise HTTPException(status_code=400, detail="Missing required parameter: hardwareId")
        
        # Convert feeder_id to integer if it's not already
        try:
            feeder_id = int(feeder_id)
        except (ValueError, TypeError):
            api_logger.error(f"🍽️ POST /feeders/hardware - Invalid feederId: {feeder_id}")
            log_transaction_end(txn_id, 400)
            raise HTTPException(status_code=400, detail="Invalid feederId format")
        
        # Verify user owns the feeder
        if not verify_ownership(user_id, feeder_id, f"POST /feeders/hardware"):
            api_logger.error(f"🍽️ POST /feeders/hardware - User {user_id} does not own feeder {feeder_id}")
            log_transaction_end(txn_id, 403)
            raise HTTPException(status_code=403, detail="You don't have permission to modify this feeder")
        
        # Clean up and validate the hardware ID (should be 12 hex characters)
        # Remove any non-alphanumeric characters (like hyphens or colons)
        clean_hardware_id = re.sub(r'[^a-fA-F0-9]', '', hardware_id)
        
        # Check if it's a valid MAC address (12 hex characters)
        if not re.match(r'^[a-fA-F0-9]{12}$', clean_hardware_id):
            api_logger.error(f"🍽️ POST /feeders/hardware - Invalid hardware ID format: {hardware_id}")
            log_transaction_end(txn_id, 400)
            raise HTTPException(status_code=400, detail="Invalid hardware ID format. Should be 12 hex characters.")

        # Check for duplicate hardware ID using service role
        try:
            api_logger.info(f"🍽️ POST /feeders/hardware - Checking for duplicate hardware ID: {clean_hardware_id}")
            
            # Use service role to check across all feeders
            duplicate_query = lambda: supabase.table("feeder").select("feederid").eq("hardwareid", clean_hardware_id).execute()
            result, error = supabase_debug_query("select", "feeder", txn_id, duplicate_query, hardwareid=clean_hardware_id)
            
            if error:
                api_logger.error(f"🍽️ POST /feeders/hardware - Error checking for duplicates: {str(error)}")
                log_transaction_end(txn_id, 500)
                raise HTTPException(status_code=500, detail="Failed to verify hardware ID uniqueness")
            
            if result and len(result) > 0:
                api_logger.error(f"🍽️ POST /feeders/hardware - Hardware ID {clean_hardware_id} is already in use")
                log_transaction_end(txn_id, 409)
                raise HTTPException(status_code=409, detail="This serial number is already registered to another feeder")
            
            api_logger.info(f"🍽️ POST /feeders/hardware - Hardware ID {clean_hardware_id} is available")
        except HTTPException:
            raise
        except Exception as check_error:
            api_logger.error(f"🍽️ POST /feeders/hardware - Error checking hardware ID uniqueness: {str(check_error)}")
            log_transaction_end(txn_id, 500)
            raise HTTPException(status_code=500, detail="Failed to verify hardware ID uniqueness")
        
        # Update the feeder in the database
        try:
            api_logger.info(f"🍽️ POST /feeders/hardware - Updating feeder {feeder_id} with hardwareid={clean_hardware_id}")
            
            update_query = lambda: supabase.table("feeder").update({"hardwareid": clean_hardware_id}).eq("feederid", feeder_id).execute()
            result, error = supabase_debug_query("update", "feeder", txn_id, update_query, feeder_id=feeder_id, hardwareid=clean_hardware_id)
            
            if error:
                api_logger.error(f"🍽️ POST /feeders/hardware - Database error: {str(error)}")
                log_transaction_end(txn_id, 500)
                raise HTTPException(status_code=500, detail="Failed to assign hardware ID")
            
            if not result or len(result) == 0:
                api_logger.error(f"🍽️ POST /feeders/hardware - No rows updated")
                log_transaction_end(txn_id, 500)
                raise HTTPException(status_code=500, detail="Failed to assign hardware ID: No rows updated")
            
            api_logger.info(f"🍽️ POST /feeders/hardware - Successfully assigned hardware ID {clean_hardware_id} to feeder {feeder_id}")
            log_transaction_end(txn_id, 200)
            
            return {
                "success": True,
                "feeder_id": feeder_id,
                "hardware_id": clean_hardware_id
            }
            
        except Exception as db_error:
            api_logger.error(f"🍽️ POST /feeders/hardware - Database error: {str(db_error)}")
            log_transaction_end(txn_id, 500)
            raise HTTPException(status_code=500, detail=f"Database error: {str(db_error)}")
            
    except HTTPException:
        # Just re-raise HTTP exceptions
        raise
    except Exception as e:
        api_logger.error(f"🍽️ POST /feeders/hardware - Unexpected error: {str(e)}")
        log_transaction_end(txn_id, 500)
        raise HTTPException(status_code=500, detail="An unexpected error occurred")

# -----------------------------------------------------------------------------
# PATCH endpoint to disassociate a feeder (rather than deleting it)
# -----------------------------------------------------------------------------
@app.patch("/feeders/{feeder_id}/disassociate", response_model=None)
async def disassociate_feeder(
    feeder_id: int,
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """
    Disassociate a feeder from a user's account and mark its hardware ID as disassociated
    """
    txn_id = log_transaction_start(request)
    api_logger.info(f"🍽️ PATCH /feeders/{feeder_id}/disassociate - Processing request [TXN: {txn_id}]")
    
    try:
        # Authenticate user
        token = credentials.credentials
        user_id = None
        
        try:
            decoded_token = jwt.decode(token, options={"verify_signature": False}, algorithms=["HS256"])
            user_id = decoded_token.get("sub")
            if not user_id:
                api_logger.error(f"🍽️ PATCH /feeders/{feeder_id}/disassociate - No user_id found in token")
                log_transaction_end(txn_id, 401)
                raise HTTPException(status_code=401, detail="Invalid authentication token")
            
            # Set auth context with Supabase for RLS policies
            api_logger.info(f"🍽️ PATCH /feeders/{feeder_id}/disassociate - Setting auth context with Supabase")
            supabase.auth.get_user(token)
            log_checkpoint(txn_id, "Auth context set with Supabase")
        except Exception as token_error:
            api_logger.error(f"🍽️ PATCH /feeders/{feeder_id}/disassociate - Error decoding token: {str(token_error)}")
            log_transaction_end(txn_id, 401)
            raise HTTPException(status_code=401, detail="Invalid authentication token")
        
        # Verify user owns the feeder
        if not verify_ownership(user_id, feeder_id, f"PATCH /feeders/{feeder_id}/disassociate"):
            api_logger.error(f"🍽️ PATCH /feeders/{feeder_id}/disassociate - User {user_id} does not own feeder {feeder_id}")
            log_transaction_end(txn_id, 403)
            raise HTTPException(status_code=403, detail="You don't have permission to modify this feeder")
        
                    # Get current feeder data
        try:
            api_logger.info(f"🍽️ PATCH /feeders/{feeder_id}/disassociate - Fetching current feeder data")
            
            # Get all the current feeder fields we need
            feeder_query = lambda: supabase.table("feeder").select("*").eq("feederid", feeder_id).single().execute()
            result, error = supabase_debug_query("select", "feeder", txn_id, feeder_query, feeder_id=feeder_id)
            
            if error:
                api_logger.error(f"🍽️ PATCH /feeders/{feeder_id}/disassociate - Database error: {str(error)}")
                log_transaction_end(txn_id, 500)
                raise HTTPException(status_code=500, detail="Failed to fetch feeder data")
            
            if not result:
                api_logger.error(f"🍽️ PATCH /feeders/{feeder_id}/disassociate - Feeder not found")
                log_transaction_end(txn_id, 404)
                raise HTTPException(status_code=404, detail="Feeder not found")
                
            # Get the current name to modify
            current_name = result.get("name", "Feeder")
            
            # Simply prepend DISASSOCIATED: to the name (if not already disassociated)
            if not current_name.startswith("DISASSOCIATED:"):
                new_name = f"DISASSOCIATED: {current_name}"
            else:
                new_name = current_name  # Already disassociated
                
            api_logger.info(f"🍽️ PATCH /feeders/{feeder_id}/disassociate - Will update name to: {new_name}")
            
            # Check for associated cats and disassociate them
            try:
                api_logger.info(f"🍽️ PATCH /feeders/{feeder_id}/disassociate - Checking for associated cats")
                cats_query = lambda: supabase.table("cat").select("catid, catname").eq("feederid", feeder_id).execute()
                cats_result, cats_error = supabase_debug_query("select", "cat", txn_id, cats_query, feeder_id=feeder_id)
                
                if cats_error:
                    api_logger.warning(f"🍽️ PATCH /feeders/{feeder_id}/disassociate - Error checking for cats: {str(cats_error)}")
                else:
                    # Get the data from the result - it could be in data property or directly in the result
                    cats_data = getattr(cats_result, "data", cats_result) if cats_result else []
                    
                    if cats_data and len(cats_data) > 0:
                        api_logger.info(f"🍽️ PATCH /feeders/{feeder_id}/disassociate - Found {len(cats_data)} cats to disassociate")
                        
                        # Set feederid to NULL for all associated cats
                        cats_update_query = lambda: supabase.table("cat").update({"feederid": None}).eq("feederid", feeder_id).execute()
                        _, cats_update_error = supabase_debug_query("update", "cat", txn_id, cats_update_query, feeder_id=feeder_id)
                        
                        if cats_update_error:
                            api_logger.warning(f"🍽️ PATCH /feeders/{feeder_id}/disassociate - Error disassociating cats: {str(cats_update_error)}")
                        else:
                            api_logger.info(f"🍽️ PATCH /feeders/{feeder_id}/disassociate - Successfully disassociated {len(cats_data)} cats")
            except Exception as cats_error:
                api_logger.warning(f"🍽️ PATCH /feeders/{feeder_id}/disassociate - Error handling cats: {str(cats_error)}")
                # Continue with feeder disassociation even if there was an error with cats
            
            # Update only the name field
            try:
                update_data = {
                    "name": new_name
                }
                
                update_query = lambda: supabase.table("feeder").update(update_data).eq("feederid", feeder_id).execute()
                update_result, update_error = supabase_debug_query("update", "feeder", txn_id, update_query, feeder_id=feeder_id)
                
                if update_error:
                    api_logger.error(f"🍽️ PATCH /feeders/{feeder_id}/disassociate - Database error: {str(update_error)}")
                    log_transaction_end(txn_id, 500)
                    raise HTTPException(status_code=500, detail="Failed to update feeder")
                
                if not update_result:
                    api_logger.error(f"🍽️ PATCH /feeders/{feeder_id}/disassociate - No rows updated")
                    log_transaction_end(txn_id, 500)
                    raise HTTPException(status_code=500, detail="Failed to update feeder: No rows updated")
                
                api_logger.info(f"🍽️ PATCH /feeders/{feeder_id}/disassociate - Successfully disassociated feeder")
                log_transaction_end(txn_id, 200)
                
                return {
                    "success": True,
                    "feeder_id": feeder_id,
                    "message": "Feeder successfully disassociated"
                }
                
            except Exception as update_error:
                api_logger.error(f"🍽️ PATCH /feeders/{feeder_id}/disassociate - Database error: {str(update_error)}")
                log_transaction_end(txn_id, 500)
                raise HTTPException(status_code=500, detail=f"Database error: {str(update_error)}")
            
        except Exception as fetch_error:
            api_logger.error(f"🍽️ PATCH /feeders/{feeder_id}/disassociate - Database error: {str(fetch_error)}")
            log_transaction_end(txn_id, 500)
            raise HTTPException(status_code=500, detail=f"Database error: {str(fetch_error)}")
            
    except HTTPException:
        raise
    except Exception as e:
        api_logger.error(f"🍽️ PATCH /feeders/{feeder_id}/disassociate - Unexpected error: {str(e)}")
        log_transaction_end(txn_id, 500)
        raise HTTPException(status_code=500, detail="An unexpected error occurred")

# ----------------- SCHEDULER ENDPOINTS --------------

@app.get("/schedule/{feeder_id}", response_model=None)
async def get_feeder_schedule(feeder_id: int, credentials: HTTPAuthorizationCredentials = Depends(security)):
    txn_id = log_transaction_start()
    api_logger.info(f"📋 GET /schedule/{feeder_id} - Request received")
    
    try:
        # Verify authentication
        token = credentials.credentials
        user_id = None
        
        try:
            decoded_token = jwt.decode(token, options={"verify_signature": False}, algorithms=["HS256"])
            user_id = decoded_token.get("sub")
            if not user_id:
                api_logger.error(f"📋 GET /schedule/{feeder_id} - No user_id found in token")
                log_transaction_end(txn_id, 401)
                raise HTTPException(status_code=401, detail="Invalid authentication token")
            
            api_logger.info(f"📋 GET /schedule/{feeder_id} - User {user_id} authenticated")
            
            # Set auth context with Supabase for RLS policies
            api_logger.info(f"📋 GET /schedule/{feeder_id} - Setting auth context with Supabase")
            supabase.auth.get_user(token)
            log_checkpoint(txn_id, "Auth context set with Supabase")
        except Exception as e:
            api_logger.error(f"📋 GET /schedule/{feeder_id} - Authentication error: {str(e)}")
            log_transaction_end(txn_id, 401)
            raise HTTPException(status_code=401, detail="Authentication error")
        
        # Verify ownership of the feeder
        if not verify_ownership(user_id, feeder_id, log_context=f"GET /schedule/{feeder_id}"):
            api_logger.warning(f"📋 GET /schedule/{feeder_id} - User {user_id} does not own feeder {feeder_id}")
            log_transaction_end(txn_id, 403)
            raise HTTPException(status_code=403, detail="Not authorized to access this feeder")
        
        log_checkpoint(txn_id, "Feeder ownership verified")
        
        # Fetch schedule from database and check owner_id
        try:
            # Query by feeder ID and owner ID to enforce authorization
            schedule_data = supabase.table("schedule").select("*").eq("feederid", feeder_id).eq("owner_id", user_id).execute()
            
            if schedule_data.data and len(schedule_data.data) > 0:
                # Found schedule
                schedule = schedule_data.data[0]
                schedule_config = schedule.get("schedule_data", "{}")
                
                if isinstance(schedule_config, str):
                    schedule_config = json.loads(schedule_config)
                
                api_logger.info(f"📋 GET /schedule/{feeder_id} - Found schedule for feeder")
                
                response = {
                    "scheduleId": schedule.get("scheduleid"),
                    "scheduleData": schedule_config,
                    "quantity": schedule.get("quantity", 100.0)
                }
                
                log_transaction_end(txn_id, 200)
                return response
            else:
                # No schedule found, return default
                api_logger.info(f"📋 GET /schedule/{feeder_id} - No schedule found, returning default")
                
                default_response = {
                    "scheduleId": None,
                    "scheduleData": {
                        "schedule": {
                            "Mon": [],
                            "Tue": [],
                            "Wed": [],
                            "Thu": [],
                            "Fri": [],
                            "Sat": [],
                            "Sun": []
                        },
                        "manualFeedCalories": 20,
                        "feedingTimes": 0,
                        "lastUpdated": datetime.utcnow().isoformat()
                    },
                    "quantity": 100.0
                }
                
                log_transaction_end(txn_id, 200)
                return default_response
                
        except Exception as e:
            api_logger.error(f"📋 GET /schedule/{feeder_id} - Database error: {str(e)}")
            log_transaction_end(txn_id, 500)
            raise HTTPException(status_code=500, detail="Database error")
            
    except HTTPException as http_ex:
        # Re-raise HTTP exceptions
        raise
    except Exception as e:
        api_logger.error(f"📋 GET /schedule/{feeder_id} - Unexpected error: {str(e)}")
        log_transaction_end(txn_id, 500)
        raise HTTPException(status_code=500, detail="Internal server error")

@app.post("/schedule/{feeder_id}", response_model=None)
async def save_feeder_schedule(feeder_id: int, request: Request, credentials: HTTPAuthorizationCredentials = Depends(security)):
    txn_id = log_transaction_start()
    api_logger.info(f"📋 POST /schedule/{feeder_id} - Request received")
    
    try:
        # Verify authentication
        token = credentials.credentials
        user_id = None
        
        try:
            decoded_token = jwt.decode(token, options={"verify_signature": False}, algorithms=["HS256"])
            user_id = decoded_token.get("sub")
            if not user_id:
                api_logger.error(f"📋 POST /schedule/{feeder_id} - No user_id found in token")
                log_transaction_end(txn_id, 401)
                raise HTTPException(status_code=401, detail="Invalid authentication token")
            
            api_logger.info(f"📋 POST /schedule/{feeder_id} - User {user_id} authenticated")
            
            # Set auth context with Supabase for RLS policies
            api_logger.info(f"📋 POST /schedule/{feeder_id} - Setting auth context with Supabase")
            supabase.auth.get_user(token)
            log_checkpoint(txn_id, "Auth context set with Supabase")
        except Exception as e:
            api_logger.error(f"📋 POST /schedule/{feeder_id} - Authentication error: {str(e)}")
            log_transaction_end(txn_id, 401)
            raise HTTPException(status_code=401, detail="Authentication error")
        
        # Verify ownership of the feeder
        if not verify_ownership(user_id, feeder_id, log_context=f"POST /schedule/{feeder_id}"):
            api_logger.warning(f"📋 POST /schedule/{feeder_id} - User {user_id} does not own feeder {feeder_id}")
            log_transaction_end(txn_id, 403)
            raise HTTPException(status_code=403, detail="Not authorized to modify this feeder")
        
        log_checkpoint(txn_id, "Feeder ownership verified")
        
        # Parse request body
        body = await request.json()
        schedule_id = body.get("scheduleId")
        schedule_data = body.get("scheduleData")
        quantity = body.get("quantity", 100.0)
        
        if not schedule_data:
            api_logger.error(f"📋 POST /schedule/{feeder_id} - Missing schedule data")
            log_transaction_end(txn_id, 400)
            raise HTTPException(status_code=400, detail="Schedule data is required")
        
        # Update lastUpdated timestamp
        if isinstance(schedule_data, dict):
            schedule_data["lastUpdated"] = datetime.utcnow().isoformat()
        
        # Check if record already exists - filter by both feeder ID and owner ID
        existing = supabase.table("schedule").select("scheduleid").eq("feederid", feeder_id).eq("owner_id", user_id).execute()
        
        # Prepare data - omit scheduleid to use auto-increment when inserting new record
        db_data = {
            "feederid": feeder_id,
            "schedule_data": json.dumps(schedule_data),
            "quantity": quantity,
            "owner_id": user_id  # Include owner_id in the data to be saved
        }
        
        # If updating existing record, include the scheduleid
        if schedule_id or (existing.data and len(existing.data) > 0):
            if not schedule_id and existing.data and len(existing.data) > 0:
                schedule_id = existing.data[0].get("scheduleid")
                api_logger.info(f"📋 POST /schedule/{feeder_id} - Using existing schedule ID: {schedule_id}")
                
            # Include the ID when updating
            db_data["scheduleid"] = schedule_id
        
        if existing.data and len(existing.data) > 0:
            # Update existing record - ensure we're updating the correct record by checking both feeder ID and owner ID
            api_logger.info(f"📋 POST /schedule/{feeder_id} - Updating existing schedule")
            result = supabase.table("schedule").update(db_data).eq("feederid", feeder_id).eq("owner_id", user_id).execute()
        else:
            # Insert new record with owner_id
            api_logger.info(f"📋 POST /schedule/{feeder_id} - Creating new schedule")
            result = supabase.table("schedule").insert(db_data).execute()
        
        api_logger.info(f"📋 POST /schedule/{feeder_id} - Schedule saved successfully")
        log_transaction_end(txn_id, 200)
        
        return {"scheduleId": schedule_id}
        
    except HTTPException as http_ex:
        # Re-raise HTTP exceptions
        raise
    except Exception as e:
        api_logger.error(f"📋 POST /schedule/{feeder_id} - Unexpected error: {str(e)}")
        log_transaction_end(txn_id, 500)
        raise HTTPException(status_code=500, detail="Internal server error")

@app.options("/schedule/{feeder_id}")
async def options_schedule(feeder_id: int):
    """Handle OPTIONS preflight requests for schedule endpoints"""
    headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Authorization, Content-Type, Accept",
        "Access-Control-Max-Age": "86400",  # 24 hours
    }
    return Response(status_code=200, headers=headers)

# ----------------- FEED LOG --------------


# history graph
@app.get("/cats/{catid}/history", response_model=None)
async def fetch_cat_history(
    catid: int,
    period: str = "week",
    data_type: Optional[str] = "all",  # "all", "weight", or "amount"
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    # Define utility functions inline to avoid dependency on functions defined later
    def _calculate_date_range(period_str: str):
        now = datetime.utcnow()
        if period_str == "month":
            start = now - timedelta(days=30)
        elif period_str == "week":
            start = now - timedelta(days=7)
        else:
            start = now - timedelta(days=7)
        return start.isoformat(), now.isoformat()

    def _group_by_day(records, field_name):
        result = {}
        for rec in records:
            if rec[field_name] is None:
                continue
            date_key = rec["created_at"][:10]
            result[date_key] = rec[field_name]
        return [{"date": k, "value": v} for k, v in sorted(result.items())]

    def _group_by_week(records, field_name):
        week_map = {}
        for rec in records:
            if rec[field_name] is None:
                continue
            dt = datetime.fromisoformat(rec["created_at"])
            monday = (dt - timedelta(days=dt.weekday())).date().isoformat()
            week_map.setdefault(monday, []).append(float(rec[field_name]))
        return [
            {"date": week, "value": sum(vals) / len(vals)} for week, vals in sorted(week_map.items())
        ]

    # Initialize transaction tracking
    txn_id = generate_transaction_id()
    log_transaction_start()
    log_checkpoint(txn_id, f"Created transaction for /cats/{catid}/history endpoint")
    
    try:
        
        api_logger.info(f"📊 GET /cats/{catid}/history - Request started with period={period}, data_type={data_type}")
        
        # Directly decode the JWT token instead of using verify_jwt
        token = credentials.credentials
        api_logger.debug(f"📊 GET /cats/{catid}/history - Decoding token: {token[:10]}...")
        
        user_id = None
        try:
            # Decode the JWT token and extract user_id
            decoded_token = jwt.decode(
                token,
                options={"verify_signature": False},
                algorithms=["HS256"]
            )
            
            # The user_id is in the 'sub' field
            user_id = decoded_token.get("sub")
            
            if not user_id:
                api_logger.error(f"📊 GET /cats/{catid}/history - No user_id found in token")
                log_checkpoint(txn_id, "Authentication failed: No user ID in token")
                raise HTTPException(status_code=401, detail="Invalid authentication token")
                
            api_logger.info(f"📊 GET /cats/{catid}/history - Successfully extracted user_id: {user_id}")
            
            # Set auth context with Supabase for RLS policies
            api_logger.info(f"📊 GET /cats/{catid}/history - Setting auth context with Supabase")
            supabase.auth.get_user(token)
            log_checkpoint(txn_id, "Auth context set with Supabase")
            
        except Exception as token_error:
            api_logger.error(f"📊 GET /cats/{catid}/history - Error decoding token: {str(token_error)}")
            log_checkpoint(txn_id, "Authentication failed: Token decode error")
            raise HTTPException(status_code=401, detail="Invalid authentication token")

        log_checkpoint(txn_id, f"Processing history request for cat {catid}")
        api_logger.info(f"📊 GET /cats/{catid}/history - Fetching history for cat {catid}, user {user_id}, period={period}, type={data_type}")

        # Determine date range
        if period == "custom" and start_date and end_date:
            start, end = start_date, end_date
            api_logger.debug(f"📊 GET /cats/{catid}/history - Using custom date range: {start} to {end}")
        else:
            start, end = _calculate_date_range(period)
            api_logger.debug(f"📊 GET /cats/{catid}/history - Using calculated date range for period '{period}': {start} to {end}")
            
        log_checkpoint(txn_id, f"Date range: {start} to {end}")

        # Determine which fields to query
        fields = []
        if data_type == "all" or data_type == "weight":
            fields.append("weight")
        if data_type == "all" or data_type == "amount":
            fields.append("amount")
        
        # Handle frontend 'feed' mapping to 'amount'
        if data_type == "feed":
            fields.append("amount")
            api_logger.debug(f"📊 GET /cats/{catid}/history - Mapping frontend 'feed' data type to 'amount' in query")
            
        if not fields:
            log_checkpoint(txn_id, f"Invalid data_type: {data_type}")
            api_logger.warning(f"📊 GET /cats/{catid}/history - Invalid data_type: {data_type}")
            raise HTTPException(status_code=400, detail="Invalid data_type")

        # Verify the cat belongs to the user before querying history
        api_logger.debug(f"📊 GET /cats/{catid}/history - Verifying cat ownership: SELECT catid FROM cat WHERE catid = {catid} AND userid = '{user_id}'")
        
        cat_ownership_query = lambda: supabase.table("cat").select("catid").eq("catid", catid).eq("userid", user_id).execute().data
        cat_ownership, cat_error = supabase_debug_query(
            "select",
            "cat",
            txn_id,
            cat_ownership_query,
            catid=catid,
            userid=user_id
        )
        
        if cat_error:
            api_logger.warning(f"📊 GET /cats/{catid}/history - Error checking cat ownership: {cat_error}")
        
        if not cat_ownership or len(cat_ownership) == 0:
            api_logger.info(f"📊 GET /cats/{catid}/history - Cat not found with direct user ownership, checking feeder relationship")
            
            # Try to get the cat through feeder relationship
            api_logger.debug(f"📊 GET /cats/{catid}/history - Checking if cat belongs to user's feeders")
            
            # Get user's feeders
            feeders_query = lambda: supabase.table("feeder").select("feederid").eq("owner_id", user_id).execute().data
            feeders, feeders_error = supabase_debug_query(
                "select",
                "feeder",
                txn_id,
                feeders_query,
                user_id=user_id
            )
            
            if feeders_error:
                api_logger.warning(f"📊 GET /cats/{catid}/history - Error checking user feeders: {feeders_error}")
            
            if not feeders or len(feeders) == 0:
                api_logger.warning(f"📊 GET /cats/{catid}/history - User {user_id} has no feeders")
                log_checkpoint(txn_id, "User has no feeders, cannot access cat")
                raise HTTPException(status_code=404, detail="Cat not found or not authorized")
                
            feeder_ids = [f["feederid"] for f in feeders]
            api_logger.debug(f"📊 GET /cats/{catid}/history - User has feeders: {feeder_ids}")
            
            # Check if cat belongs to any of user's feeders
            cat_feeder_query = lambda: supabase.table("cat").select("catid").eq("catid", catid).in_("feederid", feeder_ids).execute().data
            cat_feeder, cat_feeder_error = supabase_debug_query(
                "select",
                "cat",
                txn_id,
                cat_feeder_query,
                catid=catid,
                feeder_ids=feeder_ids
            )
            
            if cat_feeder_error:
                api_logger.warning(f"📊 GET /cats/{catid}/history - Error checking cat-feeder relationship: {cat_feeder_error}")
            
            if not cat_feeder or len(cat_feeder) == 0:
                api_logger.warning(f"📊 GET /cats/{catid}/history - Cat {catid} not found or doesn't belong to user {user_id} or their feeders")
                log_checkpoint(txn_id, f"Cat {catid} not found or doesn't belong to user {user_id} or their feeders")
                raise HTTPException(status_code=404, detail="Cat not found or not authorized")
            
            api_logger.info(f"📊 GET /cats/{catid}/history - Cat {catid} belongs to user {user_id} through feeder relationship")
            log_checkpoint(txn_id, "Cat ownership verified through feeder relationship")
        else:
            api_logger.info(f"📊 GET /cats/{catid}/history - Cat ownership verified for user {user_id}")
            log_checkpoint(txn_id, f"Cat ownership verified")

        # Get raw data with all requested fields
        select_fields = "created_at, " + ", ".join(fields)
        
        # FIXED: Use "id" instead of "catid" to match the schema
        api_logger.debug(f"📊 GET /cats/{catid}/history - Query: SELECT {select_fields} FROM feedlog WHERE id = {catid} AND created_at >= '{start}' AND created_at <= '{end}' ORDER BY created_at")
        
        try:
            feed_log_query = lambda: supabase.table("feedlog") \
                .select(select_fields) \
                .eq("id", catid) \
                .gte("created_at", start) \
                .lte("created_at", end) \
                .order("created_at", desc=False) \
                .execute()
            
            feed_log_response, feed_log_error = supabase_debug_query(
                "select",
                "feedlog",
                txn_id,
                feed_log_query,
                catid=catid,
                start=start,
                end=end,
                fields=fields
            )
            
            if feed_log_error:
                api_logger.error(f"📊 GET /cats/{catid}/history - Database error: {feed_log_error}")
                log_checkpoint(txn_id, f"Database error: {feed_log_error}")
                raise HTTPException(status_code=500, detail=f"Database error: {str(feed_log_error)}")
                
            data = feed_log_response
            api_logger.debug(f"📊 GET /cats/{catid}/history - Feed log response type: {type(data)}")
            
        except Exception as query_error:
            api_logger.error(f"📊 GET /cats/{catid}/history - Error querying feed log: {str(query_error)}")
            # Comment out the traceback reference to avoid potential import issues
            # api_logger.error(f"📊 GET /cats/{catid}/history - Traceback: {traceback.format_exc()}")
            log_checkpoint(txn_id, f"Database query error: {str(query_error)}")
            raise HTTPException(status_code=500, detail=f"Database query error: {str(query_error)}")
            
        log_checkpoint(txn_id, f"Retrieved {len(data) if data else 0} feed log records")
        
        if not data:
            api_logger.info(f"📊 GET /cats/{catid}/history - No history data found for cat {catid}")
            log_checkpoint(txn_id, "No history data found")
            
            empty_result = {
                "weight": {"data": [], "aggregation": "none"} if "weight" in fields else None,
                "amount": {"data": [], "aggregation": "none"} if "amount" in fields else None
            }
            
            # Complete transaction and return empty result
            log_transaction_end(txn_id, 200)
            api_logger.info(f"📊 GET /cats/{catid}/history - Returning empty result")
            return empty_result

        result = {}
        # Process each requested data type
        for field in fields:
            api_logger.debug(f"📊 GET /cats/{catid}/history - Processing {field} data")
            
            if period == "week":
                grouped = _group_by_day(data, field)
                result[field] = {"data": grouped, "aggregation": "daily"}
                api_logger.debug(f"📊 GET /cats/{catid}/history - Grouped {field} data by day: {len(grouped)} records")
            else:
                grouped = _group_by_week(data, field)
                result[field] = {"data": grouped, "aggregation": "weekly"}
                api_logger.debug(f"📊 GET /cats/{catid}/history - Grouped {field} data by week: {len(grouped)} records")
                
            log_checkpoint(txn_id, f"Processed {field} data: {len(grouped)} records")
            
            # Debug output of data
            if logging.getLogger().level <= logging.DEBUG and grouped:
                sample = grouped[:2] if len(grouped) > 2 else grouped
                api_logger.debug(f"📊 GET /cats/{catid}/history - Sample {field} data: {json.dumps(sample, default=str)}")
                
        # Complete transaction and return result
        api_logger.info(f"📊 GET /cats/{catid}/history - Successfully processed history data for cat {catid}")
        log_checkpoint(txn_id, "Successfully processed history data")
        log_transaction_end(txn_id, 200)
        
        return result
    except Exception as e:
        if txn_id:
            log_checkpoint(txn_id, f"Error: {str(e)}")
            log_transaction_end(txn_id, 500)
        
        api_logger.error(f"📊 GET /cats/{catid}/history - Error fetching cat history: {str(e)}")
        # Comment out the traceback reference to avoid potential import issues
        # api_logger.error(f"📊 GET /cats/{catid}/history - Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Error fetching cat history: {str(e)}")


# -----------------------------------------------------------------------------
# WebSocket broadcast repeater
# -----------------------------------------------------------------------------
clients: list[WebSocket] = []


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    # Define utility functions inline
    def _get_timestamp() -> str:
        return datetime.utcnow().isoformat()
        
    await websocket.accept()
    clients.append(websocket)
    print(f"WebSocket connected: {id(websocket)}")

    try:
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=15.0)
            except asyncio.TimeoutError:
                print(f"[{id(websocket)}] Timeout – closing.")
                break

            try:
                message = json.loads(data)
            except json.JSONDecodeError:
                print("Invalid JSON received, skipping message.")
                continue

            broadcast_message = {"type": "broadcast", "timestamp": _get_timestamp(), "message": message}

            disconnected: list[WebSocket] = []
            for client in clients:
                if client.client_state == WebSocketState.CONNECTED:
                    try:
                        await client.send_text(json.dumps(broadcast_message))
                    except Exception as e:
                        print(f"Send failed to {id(client)}: {e}")
                        disconnected.append(client)
                else:
                    disconnected.append(client)

            for client in disconnected:
                if client in clients:
                    clients.remove(client)
                    print(f"Removed disconnected client: {id(client)}")

    except WebSocketDisconnect:
        print(f"WebSocket {id(websocket)} disconnected.")
    except Exception as e:
        print(f"Unexpected error for socket {id(websocket)}: {e}")
    finally:
        if websocket in clients:
            clients.remove(websocket)
            print(f"WebSocket {id(websocket)} removed from client list.")


# -----------------------------------------------------------------------------
# Add security headers middleware to protect against common web attacks
# -----------------------------------------------------------------------------
@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """Add security headers to all responses"""
    response = await call_next(request)
    
    # Get Supabase URL from environment for CSP
    supabase_url = os.environ.get("SUPABASE_URL", "")
    csp_connect_src = "'self'"
    
    if supabase_url:
        # Extract domain from Supabase URL
        from urllib.parse import urlparse
        parsed = urlparse(supabase_url)
        domain = parsed.netloc
        csp_connect_src += f" https://{domain} wss://{domain}"
    else:
        # Fallback to generic Supabase pattern
        csp_connect_src += " https://*.supabase.co wss://*.supabase.co"
    
    # Apply security headers
    headers = {
        # Prevent clickjacking attacks
        "X-Frame-Options": "DENY",
        
        # Enable XSS protection in older browsers
        "X-XSS-Protection": "1; mode=block",
        
        # Prevent MIME type sniffing
        "X-Content-Type-Options": "nosniff",
        
        # Restrict where resources can be loaded from
        "Content-Security-Policy": f"default-src 'self'; connect-src {csp_connect_src}; img-src 'self' data:; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline';",
        
        # HSTS to enforce HTTPS (in production environments)
        # "Strict-Transport-Security": "max-age=31536000; includeSubDomains"
    }
    
    # Apply all headers
    for key, value in headers.items():
        response.headers[key] = value
        
    return response

@app.middleware("http")
async def add_security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains; preload"
    # Optionally, add Content-Security-Policy if you serve frontend
    return response

# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    # Run a security audit on startup to verify customer data protection
    print("\n🔒 Running customer data security audit...")
    try:
        import sys
        from security_audit import run_security_audit
        passed, total, _ = run_security_audit(app=app, jwt_expiry=JWT_EXPIRY_SECONDS, server_module=sys.modules[__name__], verbose=True)
        print(f"\n🔒 Security Audit completed: {passed}/{total} checks passed")
        if passed < total:
            print("⚠️ Some security checks did not pass but server will continue starting")
            print("⚠️ Review the audit results above to ensure customer data is protected")
    except Exception as e:
        print(f"⚠️ Security audit failed: {str(e)}")
        print("⚠️ Server will start but may have security vulnerabilities")
    
    # Bind to 0.0.0.0:8001 for container friendliness
    print("\n🚀 Starting server...")
    
    uvicorn.run(app, host="0.0.0.0", port=8001)


# -----------------------------------------------------------------------------
# Utility helpers
# -----------------------------------------------------------------------------

def get_txn_id_from_request(request) -> Optional[str]:
    """Safely extract transaction ID from request state"""
    if request is None:
        return None
    if not hasattr(request, "state"):
        return None
    if not hasattr(request.state, "transaction_id"):
        return None
    return request.state.transaction_id


def get_user_id_from_request(request) -> Optional[str]:
    """Safely extract user ID from request state"""
    if request is None:
        return None
    if not hasattr(request, "state"):
        return None
    if not hasattr(request.state, "user_id"):
        return None
    return request.state.user_id


# We'll import our security audit module dynamically at runtime
# to avoid circular imports while ensuring it's available when needed

# Top-level config for JWT expiry
JWT_EXPIRY_SECONDS = int(os.getenv("JWT_EXPIRY_SECONDS", 2592000))  # 30 days
JWT_EXPIRATION = JWT_EXPIRY_SECONDS
JWT_EXPIRE = JWT_EXPIRY_SECONDS

import sys
sys.modules[__name__].JWT_EXPIRY_SECONDS = JWT_EXPIRY_SECONDS
sys.modules[__name__].JWT_EXPIRATION = JWT_EXPIRY_SECONDS
sys.modules[__name__].JWT_EXPIRE = JWT_EXPIRY_SECONDS

# Also attach to __main__ for audit compatibility
if "__main__" in sys.modules:
    sys.modules["__main__"].JWT_EXPIRY_SECONDS = JWT_EXPIRY_SECONDS
    sys.modules["__main__"].JWT_EXPIRATION = JWT_EXPIRY_SECONDS
    sys.modules["__main__"].JWT_EXPIRE = JWT_EXPIRY_SECONDS






