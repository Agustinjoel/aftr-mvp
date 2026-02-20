import subprocess
import sys

def run():
    print("Running team_strength.py to refresh JSONs...")
    subprocess.check_call([sys.executable, "team_strength.py"])
    print("✅ Refresh done.")

if __name__ == "__main__":
    run()