#!/usr/bin/env python3
"""
Security Audit Test Script
Run this to verify the server's security measures without starting the full server
"""

import os
import sys
import time
from typing import Dict, List, Tuple, Any, Optional

# Ensure the server directory is in the path
SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
if SERVER_DIR not in sys.path:
    sys.path.append(SERVER_DIR)

# Import our security audit module
from security_audit import run_security_audit

# Set required environment variables for testing
os.environ['SUPABASE_USE_NATIVE_SSL'] = 'true'

def simulate_app():
    """Create a minimal app-like object to test with"""
    class MockApp:
        def __init__(self):
            class State:
                def __init__(self):
                    self.limiter = True
            self.state = State()
            self.middleware = []
    return MockApp()

def colorize(text: str, color_code: int) -> str:
    """Add color to terminal output"""
    return f"\033[{color_code}m{text}\033[0m"

def run_tests():
    """Run comprehensive security tests and report results"""
    print(colorize("\n🔒 SECURITY AUDIT TEST RUNNER 🔒", 35))  # Magenta
    print(colorize("============================", 35))
    
    # Create a mock app for testing
    mock_app = simulate_app()
    
    # Get the start time
    start_time = time.time()
    
    # Run the security audit
    print(colorize("\n[1/3] Running standard security audit...", 36))  # Cyan
    passed, total, results = run_security_audit(app=mock_app, verbose=True)
    
    # Print summary with timing
    elapsed = time.time() - start_time
    print(colorize(f"\n🕒 Security audit completed in {elapsed:.2f} seconds", 32))  # Green
    
    # Show a detailed report
    print(colorize("\n[2/3] Generating detailed report...", 36))  # Cyan
    print(colorize("\n📋 DETAILED SECURITY REPORT", 33))  # Yellow
    print(colorize("------------------------", 33))
    
    categories = {
        "Authentication": ["JWT expiration", "Password complexity"],
        "Input Validation": ["Input sanitization", "Rate limiting"],
        "Logging": ["Request audit logging", "Request transaction tracing"],
        "Infrastructure": ["SSL/TLS configuration", "Security headers"],
        "Data Protection": ["Sensitive data protection", "Secure fallback mechanisms"]
    }
    
    for category, keywords in categories.items():
        category_results = [r for r in results if any(k.lower() in r["name"].lower() for k in keywords)]
        if category_results:
            category_passed = sum(1 for r in category_results if r["passed"])
            category_total = len(category_results)
            category_percent = (category_passed / category_total) * 100 if category_total > 0 else 0
            
            # Color based on percentage
            color = 32 if category_percent == 100 else 33 if category_percent >= 60 else 31
            
            print(colorize(f"\n{category}:", 36))  # Cyan
            print(colorize(f"  Score: {category_passed}/{category_total} ({category_percent:.1f}%)", color))
            
            for result in category_results:
                status = "✅" if result["passed"] else "❌"
                name = result["name"]
                details = result["details"]
                status_color = 32 if result["passed"] else 31
                print(f"  {colorize(status, status_color)} {name}: {details}")
    
    # Test connections for production readiness
    print(colorize("\n[3/3] Testing production readiness...", 36))  # Cyan
    
    # Check if we're running as a service
    is_service = os.environ.get('INVOCATION_ID') is not None
    print(f"  {'✅' if is_service else '⚠️'} Running as systemd service: {is_service}")
    
    # Check for SSL certificate path
    ssl_cert_file = os.environ.get('SSL_CERT_FILE')
    print(f"  {'✅' if not ssl_cert_file else '⚠️'} Using native SSL: {ssl_cert_file is None}")
    
    # Overall summary
    print(colorize("\n🔒 OVERALL SECURITY RATING", 35))  # Magenta
    print(colorize("------------------------", 35))
    
    percent = (passed / total) * 100 if total > 0 else 0
    if percent >= 90:
        rating = "EXCELLENT"
        color = 32  # Green
    elif percent >= 75:
        rating = "GOOD"
        color = 33  # Yellow
    elif percent >= 60:
        rating = "FAIR"
        color = 33  # Yellow
    else:
        rating = "NEEDS IMPROVEMENT"
        color = 31  # Red
    
    print(colorize(f"\n{rating}: {passed}/{total} checks passed ({percent:.1f}%)", color))
    
    if percent < 100:
        print(colorize("\nRecommendations:", 36))
        for result in results:
            if not result["passed"]:
                print(f"- Fix: {result['name']}")
    
    # Purring like a kitten message
    if percent >= 90:
        print(colorize("\n😺 Your server is purring like a hot kitten! 😺", 32))
    else:
        print(colorize("\n🔧 A few more tweaks and your server will purr like a hot kitten!", 33))

if __name__ == "__main__":
    run_tests() 