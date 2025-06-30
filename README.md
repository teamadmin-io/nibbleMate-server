# NibbleMate API Server

A FastAPI service for the nibbleMate smart cat feeder that provides REST APIs for cat and feeder management.

## 🚀 Getting Started

### Installation

```bash
# Navigate to the server directory
cd server

# Install dependencies (creates a virtual environment)
pipenv install

# Configure environment (run.sh will create a template if .env doesn't exist)
# Edit .env file with your Supabase credentials
```

### Environment Configuration

The server requires Supabase credentials to function. After running `pipenv install`, you'll need to configure your `.env` file:

1. **Get your Supabase credentials**:
   - Go to your Supabase project dashboard
   - Navigate to Settings → API
   - Copy the following values:
     - **Project URL** (SUPABASE_URL)
     - **anon/public key** (SUPABASE_KEY)
     - **service_role key** (SUPABASE_SERVICE_KEY)

2. **Edit the `.env` file**:
   ```bash
   # From the server directory
   nano .env
   ```
   
   Replace the placeholder values with your actual Supabase credentials:
   ```env
   SUPABASE_URL=https://your-project-id.supabase.co
   SUPABASE_KEY=your_anon_key_here
   SUPABASE_SERVICE_KEY=your_service_key_here
   SUPABASE_USE_NATIVE_SSL=true
   ```

> **Important**: Never commit your `.env` file to version control. It contains sensitive credentials.

### Running the Server

#### Option 1: Using the run.sh script (Recommended)
```bash
# From the server directory
./run.sh
```

#### Option 2: Direct execution
```bash
# From the server directory
pipenv run python3 server.py
```

#### Option 3: Manual execution (not recommended)
```bash
# From the server directory
python3 server.py
```

> **Note:** All commands above assume you are in the `/server` directory. Using `pipenv` ensures all dependencies are installed in a virtual environment, not globally on your machine.

## 🔒 Security Features

The NibbleMate server has been designed with security as a top priority, especially for protecting customer data. Key security features include:

1. **Native SSL Handling**: Uses Supabase's built-in SSL certificate handling for secure connections.
2. **Authentication Security**:
   - Password complexity requirements with checks for length, character types, and common passwords
   - Advanced rate limiting with sliding window protection against brute force attacks
   - Token-based authentication with reasonable expiration times
   - Secure token validation and refresh mechanisms

3. **Input Validation & Sanitization**:
   - Input sanitization to prevent XSS attacks
   - Parameter validation to prevent injection attacks
   - Rate limiting on sensitive endpoints

4. **Security Headers**:
   - X-Frame-Options to prevent clickjacking
   - X-Content-Type-Options to prevent MIME type sniffing
   - X-XSS-Protection to enable browser XSS filtering
   - Content-Security-Policy to restrict resource loading

5. **Robust Error Handling**:
   - Fallback client for graceful handling of database unavailability
   - Comprehensive audit logging of security events
   - Sensitive data redaction in logs
   - Proper error chaining and response objects

6. **Request Tracing**:
   - Unique transaction IDs for all requests
   - Detailed checkpoint logging throughout request lifecycle
   - Performance monitoring with timing metrics

## 🛠️ Security Tools

### Security Audit

Run a comprehensive security audit on your server:

```bash
# From the server directory
pipenv run python3 test_security.py
```

This tool checks:
- SSL configuration
- JWT expiration settings
- Rate limiting
- Input sanitization
- Security headers
- Password policy
- Authentication system
- Secure defaults
- Sensitive data protection

### Health Check

Monitor the health of your server:

```bash
# From the server directory
pipenv run python3 healthcheck.py --verbose
```

Options:
- `--verbose`: Show detailed output
- `--threshold=X`: Set minimum passing score (default: 90)
- `--port=X`: Server port (default: 8001)
- `--host=X`: Server host (default: localhost)
- `--json`: Output in JSON format (for automation)

## 🐱 Production Deployment

### Docker (Coming Soon)

Docker support is planned for a future release.

### Manual Production Deployment

For production deployment, you can:

1. **Use the run.sh script** with proper environment configuration
2. **Set up a process manager** like supervisor or PM2
3. **Use containerization** when Docker support is available

> **Note**: Ensure your `.env` file contains production Supabase credentials and that the server is configured for your production environment.

## 📋 API Documentation

API documentation is available at `/docs` when the server is running (e.g., http://localhost:8001/docs).

## 🔍 Logging

The server uses structured logging with different log levels:
- ERROR: Critical issues that require immediate attention
- WARNING: Potential problems that should be investigated
- INFO: Normal operational information
- DEBUG: Detailed information for troubleshooting

Log levels can be configured in the server.py file.

## 📁 Directory Structure

```
seniordesign/
├── server/
│   ├── server.py        # Main server application
│   ├── run.sh           # Server startup script
│   ├── Pipfile          # Python dependencies
│   ├── .env             # Environment configuration
│   └── ...
└── logs/                # Server logs (created by run.sh)
    └── server.log
```

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## 📝 License

This project is licensed under the MIT License - see the LICENSE file for details. 