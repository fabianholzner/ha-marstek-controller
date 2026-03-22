import sys
from pathlib import Path

# Make the AppDaemon app importable directly as 'battery_controller'
sys.path.insert(0, str(Path(__file__).parent.parent / "controller"))
