#!/usr/bin/env python3
"""
NibbleMate Server Healthcheck
A lightweight production health monitoring script that can be used by
monitoring tools or container orchestration systems.

Usage:
  python healthcheck.py [--verbose] [--threshold=90]

Options:
  --verbose      Show detailed output
  --threshold=X  Set minimum passing score (default: 90)
"""

import os
import sys
import time
import json
import socket
import argparse
import subprocess
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

# Parse command line arguments
parser = argparse.ArgumentParser(description="NibbleMate server healthcheck")
parser.add_argument("--verbose", action="store_true", help="Show detailed output")
parser.add_argument("--threshold", type=int, default=90, help="Set minimum passing score (default: 90)")
parser.add_argument("--port", type=int, default=8001, help="Server port (default: 8001)")
parser.add_argument("--host", type=str, default="localhost", help="Server host (default: localhost)")
parser.add_argument("--timeout", type=int, default=5, help="Request timeout in seconds (default: 5)")
parser.add_argument("--json", action="store_true", help="Output in JSON format")
args = parser.parse_args()

def log(message, level="INFO"):
    """Log a message with timestamp"""
    if args.verbose and not args.json:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp}] {level.upper()}: {message}")

class HealthCheck:
    """Health check manager for the NibbleMate server"""
    
    def __init__(self):
        self.results = {}
        self.score = 0
        self.max_score = 0
        self.start_time = time.time()
    
    def add_result(self, check_name, status, message, weight=1):
        """Add a check result"""
        self.results[check_name] = {
            "status": status,
            "message": message,
            "weight": weight
        }
        
        if status:
            self.score += weight
        self.max_score += weight
        
        log(f"{check_name}: {'PASS' if status else 'FAIL'} - {message}")
    
    def check_api_health(self):
        """Check if the API health endpoint is responding"""
        url = f"http://{args.host}:{args.port}/health"
        log(f"Checking API health at {url}")
        
        try:
            req = Request(url)
            req.add_header('User-Agent', 'NibbleMate-Healthcheck/1.0')
            
            start = time.time()
            with urlopen(req, timeout=args.timeout) as response:
                response_time = time.time() - start
                data = json.loads(response.read().decode('utf-8'))
                
                if data.get('status') == 'healthy':
                    self.add_result(
                        "API Health", True, 
                        f"Health endpoint responded in {response_time:.2f}s", 
                        weight=3
                    )
                else:
                    self.add_result(
                        "API Health", False, 
                        f"Health endpoint returned unexpected status: {data.get('status')}", 
                        weight=3
                    )
                    
        except HTTPError as e:
            self.add_result("API Health", False, f"HTTP Error: {e.code} {e.reason}", weight=3)
        except URLError as e:
            self.add_result("API Health", False, f"URL Error: {str(e)}", weight=3)
        except Exception as e:
            self.add_result("API Health", False, f"Error: {str(e)}", weight=3)
    
    def check_port_open(self):
        """Check if the server port is open"""
        log(f"Checking if port {args.port} is open on {args.host}")
        
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(args.timeout)
            result = sock.connect_ex((args.host, args.port))
            sock.close()
            
            if result == 0:
                self.add_result("Port Check", True, f"Port {args.port} is open", weight=2)
            else:
                self.add_result("Port Check", False, f"Port {args.port} is closed (error: {result})", weight=2)
                
        except Exception as e:
            self.add_result("Port Check", False, f"Error checking port: {str(e)}", weight=2)
    
    def check_system_resources(self):
        """Check system resource usage"""
        log("Checking system resources")
        
        try:
            # Get memory usage of the Python process
            memory_usage = 0
            try:
                import psutil # type: ignore
                process = psutil.Process(os.getpid())
                memory_usage = process.memory_info().rss / (1024 * 1024)  # Convert to MB
                self.add_result(
                    "System Resources", True, 
                    f"Memory usage: {memory_usage:.1f} MB", 
                    weight=1
                )
            except ImportError:
                # psutil not available, try using ps command
                try:
                    # Find the server process
                    ps_output = subprocess.check_output(
                        f"ps aux | grep '[p]ython.*server.py'", 
                        shell=True
                    ).decode('utf-8')
                    
                    # Basic parsing of ps output
                    if ps_output:
                        parts = ps_output.strip().split()
                        if len(parts) > 5:  # ps aux format has memory % at index 3
                            memory_percent = float(parts[3])
                            self.add_result(
                                "System Resources", True, 
                                f"Memory usage: {memory_percent:.1f}%", 
                                weight=1
                            )
                    else:
                        self.add_result(
                            "System Resources", False, 
                            "Server process not found", 
                            weight=1
                        )
                except Exception as e:
                    self.add_result(
                        "System Resources", False, 
                        f"Could not check system resources: {str(e)}", 
                        weight=1
                    )
                    
        except Exception as e:
            self.add_result("System Resources", False, f"Error checking resources: {str(e)}", weight=1)
    
    def check_ssl_environment(self):
        """Check SSL environment configuration"""
        log("Checking SSL environment")
        
        ssl_native = os.environ.get('SUPABASE_USE_NATIVE_SSL') == 'true'
        ssl_cert_file = os.environ.get('SSL_CERT_FILE')
        
        if ssl_native:
            self.add_result("SSL Configuration", True, "Using native SSL handling", weight=1)
        elif ssl_cert_file and os.path.exists(ssl_cert_file):
            self.add_result("SSL Configuration", True, f"Using custom SSL cert: {ssl_cert_file}", weight=1)
        elif ssl_cert_file:
            self.add_result("SSL Configuration", False, f"SSL cert file doesn't exist: {ssl_cert_file}", weight=1)
        else:
            self.add_result("SSL Configuration", False, "No SSL configuration found", weight=1)
    
    def run_all_checks(self):
        """Run all health checks"""
        self.check_port_open()
        self.check_api_health()
        self.check_system_resources()
        self.check_ssl_environment()
    
    def get_score(self):
        """Get the health score as a percentage"""
        if self.max_score == 0:
            return 0
        return (self.score / self.max_score) * 100
    
    def get_status(self):
        """Get overall status based on threshold"""
        score = self.get_score()
        if score >= args.threshold:
            return "healthy"
        else:
            return "unhealthy"
    
    def get_summary_json(self):
        """Get a summary as a JSON-serializable dictionary"""
        elapsed = time.time() - self.start_time
        score = self.get_score()
        status = self.get_status()
        
        return {
            "status": status,
            "score": round(score, 1),
            "elapsed_seconds": round(elapsed, 3),
            "checks": self.results,
            "threshold": args.threshold
        }
        
    def get_summary_text(self):
        """Get a summary as a formatted text string"""
        elapsed = time.time() - self.start_time
        score = self.get_score()
        status = self.get_status()
        
        passed = sum(1 for r in self.results.values() if r["status"])
        total = len(self.results)
        
        if score >= 90:
            emoji = "😺"  # Purring like a kitten
        elif score >= 75:
            emoji = "😼"  # Content cat
        elif score >= 50:
            emoji = "😿"  # Sad cat
        else:
            emoji = "🙀"  # Scared cat
            
        summary = [
            f"\n{emoji} Health Check Summary {emoji}",
            f"Status: {status.upper()}",
            f"Score: {score:.1f}% ({self.score}/{self.max_score})",
            f"Checks Passed: {passed}/{total}",
            f"Completed in: {elapsed:.3f} seconds",
            "\nDetailed Results:"
        ]
        
        for name, result in self.results.items():
            status_str = "✅ PASS" if result["status"] else "❌ FAIL"
            summary.append(f"  {status_str} {name} (weight: {result['weight']})")
            summary.append(f"       {result['message']}")
        
        if score >= args.threshold:
            summary.append("\n🔊 Your server is purring like a hot kitten! 🔊")
        else:
            summary.append("\n⚠️ Your server needs attention! ⚠️")
            
        return "\n".join(summary)
        
    def get_summary(self):
        """Get a summary in the appropriate format based on output mode"""
        if args.json:
            return self.get_summary_json()
        else:
            return self.get_summary_text()

def main():
    """Main function"""
    health_check = HealthCheck()
    health_check.run_all_checks()
    
    # Get the health status directly from the method, not from the result
    status = health_check.get_status()
    
    if args.json:
        # Get the JSON data and print it
        result_dict = health_check.get_summary_json()
        print(json.dumps(result_dict, indent=2))
    else:
        # Get the formatted text and print it
        result_str = health_check.get_summary_text()
        print(result_str)
    
    # Use the status we got directly for the exit code
    sys.exit(0 if status == "healthy" else 1)

if __name__ == "__main__":
    main() 