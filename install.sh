#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
#  Clinical Dictaphone — Installer
#  Replaces physical dictaphone hardware with iPhone/Android.
#  Installs the server + registers as a system service.
# ═══════════════════════════════════════════════════════════════

set -e

APP_NAME="dictaphone-hl7"
INSTALL_DIR="/opt/$APP_NAME"
SERVICE_NAME="dictaphone"
PORT="${PORT:-3000}"
WORKLIST_PORT="${WORKLIST_PORT:-2576}"
CURRENT_USER="$(whoami)"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo ""
echo -e "${BLUE}═══════════════════════════════════════════════════${NC}"
echo -e "${BLUE}   Clinical Dictaphone — HL7 Installer${NC}"
echo -e "${BLUE}   Phone replaces physical dictaphone hardware${NC}"
echo -e "${BLUE}═══════════════════════════════════════════════════${NC}"
echo ""

# ── Check prerequisites ──────────────────────────────────────
echo -e "${YELLOW}[1/6]${NC} Checking prerequisites..."

# Node.js
if ! command -v node &> /dev/null; then
    echo -e "${RED}ERROR: Node.js is not installed.${NC}"
    echo ""
    echo "Install Node.js first:"
    echo "  Ubuntu/Debian:  sudo apt install -y nodejs npm"
    echo "  RHEL/CentOS:    sudo yum install -y nodejs npm"
    echo "  macOS:          brew install node"
    echo "  Or download:    https://nodejs.org/"
    echo ""
    exit 1
fi

NODE_VER=$(node -v | cut -d'v' -f2 | cut -d'.' -f1)
if [ "$NODE_VER" -lt 14 ]; then
    echo -e "${RED}ERROR: Node.js v14+ required (found v$(node -v)).${NC}"
    exit 1
fi
echo -e "  Node.js $(node -v) ${GREEN}OK${NC}"

# npm
if ! command -v npm &> /dev/null; then
    echo -e "${RED}ERROR: npm is not installed.${NC}"
    exit 1
fi
echo -e "  npm $(npm -v) ${GREEN}OK${NC}"

# ── Copy application files ───────────────────────────────────
echo -e "${YELLOW}[2/6]${NC} Installing application to ${INSTALL_DIR}..."

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ "$EUID" -eq 0 ] || sudo -n true 2>/dev/null; then
    sudo mkdir -p "$INSTALL_DIR"
    sudo cp "$SCRIPT_DIR/server.js" "$INSTALL_DIR/"
    sudo cp "$SCRIPT_DIR/hl7.js" "$INSTALL_DIR/"
    sudo cp "$SCRIPT_DIR/worklist.js" "$INSTALL_DIR/"
    sudo cp "$SCRIPT_DIR/package.json" "$INSTALL_DIR/"
    sudo cp -r "$SCRIPT_DIR/public" "$INSTALL_DIR/"
    sudo chown -R "$CURRENT_USER:$CURRENT_USER" "$INSTALL_DIR" 2>/dev/null || true
    echo -e "  Files copied ${GREEN}OK${NC}"
else
    # No sudo — install to user directory
    INSTALL_DIR="$HOME/$APP_NAME"
    mkdir -p "$INSTALL_DIR"
    cp "$SCRIPT_DIR/server.js" "$INSTALL_DIR/"
    cp "$SCRIPT_DIR/hl7.js" "$INSTALL_DIR/"
    cp "$SCRIPT_DIR/worklist.js" "$INSTALL_DIR/"
    cp "$SCRIPT_DIR/package.json" "$INSTALL_DIR/"
    cp -r "$SCRIPT_DIR/public" "$INSTALL_DIR/"
    echo -e "  Files copied to ${INSTALL_DIR} ${GREEN}OK${NC}"
fi

# ── Install dependencies ─────────────────────────────────────
echo -e "${YELLOW}[3/6]${NC} Installing Node.js dependencies..."
cd "$INSTALL_DIR"
npm install --production --silent 2>&1
echo -e "  Dependencies ${GREEN}OK${NC}"

# ── Create systemd service (Linux) ───────────────────────────
echo -e "${YELLOW}[4/6]${NC} Setting up system service..."

if [ -d /etc/systemd/system ] && ([ "$EUID" -eq 0 ] || sudo -n true 2>/dev/null); then
    sudo tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null <<SERVICEEOF
[Unit]
Description=Clinical Dictaphone HL7 Server
After=network.target

[Service]
Type=simple
User=${CURRENT_USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=$(which node) ${INSTALL_DIR}/server.js
Restart=always
RestartSec=5
Environment=PORT=${PORT}
Environment=WORKLIST_PORT=${WORKLIST_PORT}
Environment=NODE_ENV=production

# Security hardening
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=${INSTALL_DIR}

[Install]
WantedBy=multi-user.target
SERVICEEOF

    sudo systemctl daemon-reload
    sudo systemctl enable ${SERVICE_NAME} 2>/dev/null
    sudo systemctl restart ${SERVICE_NAME}
    echo -e "  Service ${SERVICE_NAME} ${GREEN}enabled and started${NC}"
    SERVICE_INSTALLED=true
else
    echo -e "  ${YELLOW}Skipped${NC} (no systemd or no sudo). Start manually:"
    echo "    cd $INSTALL_DIR && node server.js"
    SERVICE_INSTALLED=false
fi

# ── Detect server IP ─────────────────────────────────────────
echo -e "${YELLOW}[5/6]${NC} Detecting network..."

SERVER_IP=""
if command -v ip &> /dev/null; then
    SERVER_IP=$(ip -4 route get 1.1.1.1 2>/dev/null | grep -oP 'src \K[0-9.]+' || true)
elif command -v ifconfig &> /dev/null; then
    SERVER_IP=$(ifconfig | grep 'inet ' | grep -v '127.0.0.1' | head -1 | awk '{print $2}' | sed 's/addr://')
fi

if [ -z "$SERVER_IP" ]; then
    SERVER_IP="<your-server-ip>"
fi
echo -e "  Server IP: ${GREEN}${SERVER_IP}${NC}"

# ── Open firewall ports ──────────────────────────────────────
echo -e "${YELLOW}[6/6]${NC} Firewall..."

if command -v ufw &> /dev/null && ([ "$EUID" -eq 0 ] || sudo -n true 2>/dev/null); then
    sudo ufw allow ${PORT}/tcp comment "Dictaphone Web UI" 2>/dev/null || true
    sudo ufw allow ${WORKLIST_PORT}/tcp comment "Dictaphone MLLP Listener" 2>/dev/null || true
    echo -e "  Ports ${PORT}, ${WORKLIST_PORT} ${GREEN}opened${NC}"
elif command -v firewall-cmd &> /dev/null && ([ "$EUID" -eq 0 ] || sudo -n true 2>/dev/null); then
    sudo firewall-cmd --permanent --add-port=${PORT}/tcp 2>/dev/null || true
    sudo firewall-cmd --permanent --add-port=${WORKLIST_PORT}/tcp 2>/dev/null || true
    sudo firewall-cmd --reload 2>/dev/null || true
    echo -e "  Ports ${PORT}, ${WORKLIST_PORT} ${GREEN}opened${NC}"
else
    echo -e "  ${YELLOW}Skipped${NC} (configure manually if needed)"
fi

# ── Done ─────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════${NC}"
echo -e "${GREEN}   Installation complete!${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════${NC}"
echo ""
echo -e "  ${BLUE}Server:${NC}     http://${SERVER_IP}:${PORT}"
echo -e "  ${BLUE}Worklist:${NC}   MLLP port ${WORKLIST_PORT} (receives orders from RIS)"
echo -e "  ${BLUE}Install dir:${NC} ${INSTALL_DIR}"
echo ""
echo -e "  ${YELLOW}── Phone Setup ──${NC}"
echo ""
echo "  1. Connect your iPhone/Android to the same WiFi network"
echo "  2. Open Safari (iPhone) or Chrome (Android)"
echo "  3. Go to:  http://${SERVER_IP}:${PORT}"
echo "  4. Install as app:"
echo "     iPhone:  Share > Add to Home Screen"
echo "     Android: Menu > Add to Home Screen / Install App"
echo "  5. Open the app > Settings > Enter your Badge Number"
echo ""

if [ "$SERVICE_INSTALLED" = true ]; then
    echo -e "  ${YELLOW}── Service Commands ──${NC}"
    echo ""
    echo "  sudo systemctl status  ${SERVICE_NAME}   # Check status"
    echo "  sudo systemctl restart ${SERVICE_NAME}   # Restart"
    echo "  sudo systemctl stop    ${SERVICE_NAME}   # Stop"
    echo "  sudo journalctl -u     ${SERVICE_NAME}   # View logs"
    echo ""
fi

echo -e "  ${YELLOW}── RIS Configuration ──${NC}"
echo ""
echo "  Point your RIS (GE Centricity) to send orders to:"
echo "    Host: ${SERVER_IP}    Port: ${WORKLIST_PORT}    (HL7 ORM^O01)"
echo ""
echo "  The dictaphone sends results back to RIS as HL7 ORU^R01."
echo "  Configure the RIS host/port in the phone app Settings."
echo ""
