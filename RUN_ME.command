#!/bin/bash
# STEAMery Grant Discovery Tool — Double-click launcher for Mac
# This installs what's needed and runs the tool. Open report.html when done.

cd "$(dirname "$0")"

echo "======================================"
echo "  STEAMery Grant Discovery Tool"
echo "======================================"
echo ""

# Check Python 3
if ! command -v python3 &> /dev/null; then
    echo "ERROR: Python 3 is not installed."
    echo "Download it from https://www.python.org/downloads/ then try again."
    read -p "Press Enter to close..."
    exit 1
fi

echo "Installing required libraries..."
pip3 install requests lxml --quiet

echo ""
echo "Running the grant discovery tool..."
echo "(This takes 20-40 minutes — leave this window open.)"
echo ""

python3 steamery_grant_finder.py

echo ""
echo "Done! Opening report in your browser..."
open report.html

read -p "Press Enter to close this window..."
