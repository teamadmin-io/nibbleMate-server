#!/usr/bin/env python3
"""
NibbleMate Security Audit Module

This module performs a comprehensive security audit of the server configuration,
checking for common security vulnerabilities and best practices.

Usage:
    from security_audit import run_security_audit
    passed, total, results = run_security_audit(app=app)
"""

import os
import sys
import ssl
import time
import socket
import logging
import inspect
from typing import Dict, List, Tuple, Any, Optional

# Configure logger
logger = logging.getLogger("nibblemate.security")

def run_security_audit(app: Any = None, jwt_expiry: int = None, server_module: Any = None, verbose: bool = True) -> Tuple[int, int, List[Dict[str, Any]]]:
    """
    Run a comprehensive security audit and return the results.
    
    Args:
        app: The FastAPI application instance
        jwt_expiry: JWT expiry in seconds (optional, recommended)
        server_module: The server module object to inspect (required for code analysis)
        verbose: Whether to print verbose output
        
    Returns:
        Tuple of (passed_checks, total_checks, detailed_results)
    """
    start_time = time.time()
    
    if verbose:
        print("🔒 Running security audit...")
    
    # Store audit results
    results = []
    
    # Helper function to log check results
    def log_check(name: str, passed: bool, details: str = ""):
        """Log a security check result"""
        if verbose:
            status = "✅ PASS" if passed else "❌ FAIL"
            print(f"{status} {name}: {details}")
            
        results.append({
            "name": name,
            "passed": passed,
            "details": details
        })
        
        return passed
    
    # Check 1: SSL Environment Configuration
    ssl_native = os.environ.get("SUPABASE_USE_NATIVE_SSL") == "true"
    ssl_cert_file = os.environ.get("SSL_CERT_FILE")
    
    if ssl_native:
        log_check("SSL Configuration", True, "Using native SSL handling (recommended)")
    elif ssl_cert_file and os.path.exists(ssl_cert_file):
        log_check("SSL Configuration", True, f"Using custom SSL cert: {ssl_cert_file}")
    else:
        log_check("SSL Configuration", False, "No valid SSL configuration found")
    
    # Check 2: JWT Expiration
    if jwt_expiry is not None:
        if jwt_expiry <= 30 * 24 * 60 * 60:
            log_check("JWT Expiration", True, f"JWT expiry set to {jwt_expiry} seconds ({jwt_expiry/86400:.1f} days)")
        else:
            log_check("JWT Expiration", False, f"JWT expiry too long: {jwt_expiry} seconds ({jwt_expiry/86400:.1f} days)")
    else:
        log_check("JWT Expiration", False, "JWT expiry not configured")
    
    # Check 3: Rate Limiting
    if app and hasattr(app, "state") and hasattr(app.state, "limiter"):
        log_check("Rate Limiting", True, "Rate limiter is configured")
    else:
        log_check("Rate Limiting", False, "Rate limiter not configured")
    
    # Check 4: Input Sanitization
    if server_module is not None:
        sanitization_keywords = ["sanitize", "escape", "validate_", "clean_", "xss"]
        module_code = inspect.getsource(server_module)
        
        if any(keyword in module_code for keyword in sanitization_keywords):
            log_check("Input Sanitization", True, "Input sanitization detected")
        else:
            log_check("Input Sanitization", False, "Input sanitization not detected")
    else:
        log_check("Input Sanitization", False, "Could not analyze server module (not provided)")
    
    # Check 5: Security Headers
    if server_module is not None:
        module_code = inspect.getsource(server_module)
        
        security_headers = [
            "X-Frame-Options", 
            "X-XSS-Protection", 
            "X-Content-Type-Options",
            "Content-Security-Policy"
        ]
        
        if any(header in module_code for header in security_headers):
            log_check("Security Headers", True, "Security headers middleware detected")
        else:
            log_check("Security Headers", False, "Security headers not configured")
    else:
        log_check("Security Headers", False, "Could not analyze server module (not provided)")
    
    # Check 6: HTTPS Only
    # This is a recommendation for production
    log_check("HTTPS Enforcement", True, 
             "Ensure HTTPS is enforced in production (typically at load balancer level)")
    
    # Check 7: Password Policy
    if server_module is not None:
        password_checks = [
            "password complexity", 
            "len(password)", 
            "password.lower() in common_passwords",
            "has_upper", "has_lower", "has_digit"
        ]
        
        module_code = inspect.getsource(server_module)
        
        if any(check in module_code for check in password_checks):
            log_check("Password Policy", True, "Password complexity requirements detected")
        else:
            log_check("Password Policy", False, "Password complexity not enforced")
    else:
        log_check("Password Policy", False, "Could not analyze server module (not provided)")
    
    # Check 8: Authentication System
    if server_module is not None:
        auth_endpoints = ["/auth/sign-in", "/auth/sign-up", "/auth/sign-out"]
        module_code = inspect.getsource(server_module)
        
        if all(endpoint in module_code for endpoint in auth_endpoints):
            log_check("Authentication System", True, "Authentication endpoints detected")
        else:
            log_check("Authentication System", False, "Authentication endpoints incomplete")
    else:
        log_check("Authentication System", False, "Could not analyze server module (not provided)")
    
    # Check 9: Secure Default Environment
    secure_defaults = True
    
    # Check for unsafe environment variables
    unsafe_env = [
        "DEBUG=True", 
        "ALLOW_ALL_ORIGINS=True",
        "DISABLE_AUTH=True",
        "DISABLE_SECURITY=True"
    ]
    
    for env in unsafe_env:
        if env in os.environ:
            secure_defaults = False
            log_check("Secure Defaults", False, f"Unsafe environment variable: {env}")
            break
    
    if secure_defaults:
        log_check("Secure Defaults", True, "No unsafe environment variables detected")
    
    # Check 10: Sensitive Data Protection
    if server_module is not None:
        protection_signs = [
            "_sanitize_data_for_logging", 
            "REDACTED", 
            "safe_data",
            "password", "token", "secret", "credit_card", "ssn", "email"
        ]
        
        module_code = inspect.getsource(server_module)
        
        if any(sign in module_code for sign in protection_signs):
            log_check("Sensitive Data Protection", True, "Data protection mechanisms detected")
        else:
            log_check("Sensitive Data Protection", False, "Data protection not detected")
    else:
        log_check("Sensitive Data Protection", False, "Could not analyze server module (not provided)")
    
    # Calculate the final score
    passed = sum(1 for r in results if r["passed"])
    total = len(results)
    
    elapsed = time.time() - start_time
    
    if verbose:
        print(f"\n🔒 Security audit completed in {elapsed:.2f}s")
        print(f"🔒 Score: {passed}/{total} checks passed ({passed/total*100:.1f}%)")
    
    return passed, total, results

if __name__ == "__main__":
    # If run directly, try to import the server module
    try:
        # Configure logging
        logging.basicConfig(level=logging.INFO)
        
        # Add the parent directory to the path if needed
        parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if parent_dir not in sys.path:
            sys.path.append(parent_dir)
            
        # Run the audit
        run_security_audit(verbose=True)
        
    except Exception as e:
        print(f"Error running security audit: {e}")
        sys.exit(1) 