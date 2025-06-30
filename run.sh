#!/bin/bash

# Exit on any error
set -e

# Configuration
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
LOG_DIR="${SCRIPT_DIR}/../logs"
LOG_FILE="${LOG_DIR}/server.log"
PYTHON_VERSION="3.12"  # Minimum required Python version

# Environment file
SERVER_ENV="${SCRIPT_DIR}/.env"

# Required environment variables
REQUIRED_ENV_VARS=("SUPABASE_URL" "SUPABASE_KEY" "SUPABASE_SERVICE_KEY")

# Process management configuration
MAX_RESTARTS=3
RESTART_DELAY=5
SERVER_PORT=8001
HEALTH_CHECK_TIMEOUT=30

# Logging configuration
MAX_LOG_SIZE=10485760  # 10MB
MAX_LOG_FILES=5
LOG_LEVEL="INFO"  # DEBUG, INFO, WARNING, ERROR

# System requirements
MIN_MEMORY=512  # MB
MIN_DISK_SPACE=1024  # MB

# Create logs directory if it doesn't exist
mkdir -p "${LOG_DIR}"

# Function to log messages with levels
log() {
    local level=$1
    shift
    local message=$*
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    
    # Check if message level is enabled
    case $LOG_LEVEL in
        DEBUG) levels=("DEBUG" "INFO" "WARNING" "ERROR") ;;
        INFO) levels=("INFO" "WARNING" "ERROR") ;;
        WARNING) levels=("WARNING" "ERROR") ;;
        ERROR) levels=("ERROR") ;;
        *) levels=("INFO" "WARNING" "ERROR") ;;
    esac
    
    if [[ " ${levels[@]} " =~ " ${level} " ]]; then
        echo "[${timestamp}] [${level}] ${message}" | tee -a "${LOG_FILE}"
    fi
}

# Function to check environment file
check_env_file() {
    if [ ! -f "${SERVER_ENV}" ]; then
        log "ERROR" "Server .env file not found at ${SERVER_ENV}"
        log "INFO" "Creating template server .env file..."
        cat > "${SERVER_ENV}" << EOL
SUPABASE_URL=your_supabase_url
SUPABASE_KEY=your_anon_key
SUPABASE_SERVICE_KEY=your_service_key
SUPABASE_USE_NATIVE_SSL=true
EOL
        log "INFO" "Please update ${SERVER_ENV} with your Supabase credentials"
        exit 1
    fi

    # Load environment variables
    set -a
    source "${SERVER_ENV}"
    set +a

    # Verify environment variables
    for var in "${REQUIRED_ENV_VARS[@]}"; do
        if [ -z "${!var}" ]; then
            log "ERROR" "Required environment variable ${var} is not set in ${SERVER_ENV}"
            exit 1
        fi
    done

    log "DEBUG" "Environment file check passed"
}

# Function to rotate logs
rotate_logs() {
    if [ -f "${LOG_FILE}" ] && [ $(stat -f%z "${LOG_FILE}") -gt ${MAX_LOG_SIZE} ]; then
        log "INFO" "Rotating log files..."
        for i in $(seq ${MAX_LOG_FILES} -1 1); do
            if [ -f "${LOG_FILE}.${i}" ]; then
                mv "${LOG_FILE}.${i}" "${LOG_FILE}.$((i+1))"
            fi
        done
        mv "${LOG_FILE}" "${LOG_FILE}.1"
        touch "${LOG_FILE}"
        log "INFO" "Log rotation complete"
    fi
}

# Function to check Python version
check_python_version() {
    if ! command -v python3 &> /dev/null; then
        log "ERROR" "Python 3 is not installed"
        exit 1
    fi
    
    PYTHON_VERSION_ACTUAL=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
    if [ "$(printf '%s\n' "${PYTHON_VERSION}" "${PYTHON_VERSION_ACTUAL}" | sort -V | head -n1)" != "${PYTHON_VERSION}" ]; then
        log "ERROR" "Python ${PYTHON_VERSION} or higher is required. Found ${PYTHON_VERSION_ACTUAL}"
        exit 1
    fi
    log "DEBUG" "Python version check passed: ${PYTHON_VERSION_ACTUAL}"
}

# Function to check system resources
check_system_resources() {
    # Check memory
    local available_memory=$(free -m | awk '/^Mem:/{print $7}')
    if [ $available_memory -lt $MIN_MEMORY ]; then
        log "WARNING" "Low memory available (${available_memory}MB). Minimum recommended: ${MIN_MEMORY}MB"
    fi
    
    # Check disk space
    local available_disk=$(df -m "${SCRIPT_DIR}" | awk 'NR==2 {print $4}')
    if [ $available_disk -lt $MIN_DISK_SPACE ]; then
        log "WARNING" "Low disk space available (${available_disk}MB). Minimum recommended: ${MIN_DISK_SPACE}MB"
    fi
    
    log "DEBUG" "System resources check completed"
}

# Function to check virtual environment
check_virtualenv() {
    if [ -z "$VIRTUAL_ENV" ]; then
        log "WARNING" "No virtual environment detected"
    else
        log "DEBUG" "Virtual environment detected: ${VIRTUAL_ENV}"
    fi
}

# Function to validate input
validate_input() {
    local input=$1
    local valid_options=$2
    
    if [[ ! $input =~ ^[$valid_options]$ ]]; then
        log "ERROR" "Invalid input. Please enter one of: ${valid_options}"
        return 1
    fi
    return 0
}

# Function to check if server is already running
check_server_running() {
    if pgrep -f "python3.*server.py" > /dev/null; then
        log "WARNING" "Server is already running"
        read -p "Do you want to stop the existing server and start a new one? (y/n) " -n 1 -r
        echo
        if validate_input "$REPLY" "yYnN"; then
            if [[ $REPLY =~ ^[Yy]$ ]]; then
                pkill -f "python3.*server.py"
                sleep 2
            else
                exit 0
            fi
        else
            exit 1
        fi
    fi
}

# Function to install pipenv if not present
install_pipenv() {
    if ! command -v pipenv &> /dev/null; then
        log "INFO" "pipenv not found. Installing pipenv..."
        
        if ! command -v pip &> /dev/null; then
            log "ERROR" "pip not found. Please install pip first."
            exit 1
        fi
        
        pip install --user pipenv
        export PATH="$HOME/.local/bin:$PATH"
        
        if ! command -v pipenv &> /dev/null; then
            log "ERROR" "Failed to install pipenv"
            exit 1
        fi
    else
        log "DEBUG" "pipenv is already installed."
    fi
}

# Function to validate dependencies
validate_dependencies() {
    if [ ! -f "${SCRIPT_DIR}/Pipfile.lock" ]; then
        log "WARNING" "No Pipfile.lock found. Dependencies may not be pinned."
    else
        log "DEBUG" "Pipfile.lock found"
    fi
}

# Function to install dependencies
install_dependencies() {
    log "INFO" "Installing dependencies (pipenv)..."
    validate_dependencies
    
    local retry_count=0
    local max_retries=3
    
    while [ $retry_count -lt $max_retries ]; do
        if pipenv install; then
            log "INFO" "Dependencies installed successfully"
            return 0
        fi
        
        retry_count=$((retry_count+1))
        if [ $retry_count -lt $max_retries ]; then
            log "WARNING" "Failed to install dependencies. Retrying (${retry_count}/${max_retries})..."
            sleep 5
        fi
    done
    
    log "ERROR" "Failed to install dependencies after ${max_retries} attempts"
    exit 1
}

# Function to check server health
check_server_health() {
    local retries=5
    local wait_time=2
    local i=0
    
    while [ $i -lt $retries ]; do
        if curl -s "http://localhost:${SERVER_PORT}/health" > /dev/null; then
            log "DEBUG" "Server health check passed"
            return 0
        fi
        sleep $wait_time
        i=$((i+1))
    done
    log "ERROR" "Server health check failed after ${retries} attempts"
    return 1
}

# Function to start the server
start_server() {
    log "INFO" "Starting server..."
    exec pipenv run python3 server.py
}

# Function to handle script termination
cleanup() {
    if [ -f "${SCRIPT_DIR}/server.pid" ]; then
        PID=$(cat "${SCRIPT_DIR}/server.pid")
        if kill -0 ${PID} 2>/dev/null; then
            log "INFO" "Stopping server (PID: ${PID})..."
            kill ${PID}
            rm "${SCRIPT_DIR}/server.pid"
        fi
    fi
}

# Set up trap for cleanup
trap cleanup EXIT

# Main execution
log "INFO" "Starting server initialization..."

# Run checks
check_python_version
check_env_file
check_system_resources
check_virtualenv
check_server_running
install_pipenv
install_dependencies

# Start server
start_server

# Keep script running and handle signals
log "INFO" "Server is running. Press Ctrl+C to stop."
wait
