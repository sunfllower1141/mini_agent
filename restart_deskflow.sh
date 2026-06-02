#!/bin/bash
# Restart Deskflow when mouse stops crossing between screens
killall Deskflow && sleep 1 && open -a Deskflow
echo "Deskflow restarted"
