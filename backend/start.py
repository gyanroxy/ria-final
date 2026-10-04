import subprocess
import sys
import time
import os

def run():
    print("🚀 Starting RIA Voice Agent Backend...")
    
    base_dir = os.path.dirname(os.path.abspath(__file__))
    agent_path = os.path.join(base_dir, "agent.py")
    server_path = os.path.join(base_dir, "server.py")

    # 1. Start the LiveKit Voice Agent worker
    print("🎙️ Launching LiveKit Agent worker (agent.py)...")
    agent_process = subprocess.Popen([sys.executable, agent_path, "start"], cwd=base_dir)

    # 2. Start the Flask HTTP Health Check server on the assigned PORT
    print("🌐 Launching Health Check server (server.py)...")
    server_process = subprocess.Popen([sys.executable, server_path], cwd=base_dir)

    try:
        # Keep both processes running and monitor them
        while True:
            time.sleep(2)
            if agent_process.poll() is not None:
                print("⚠️ LiveKit Agent process stopped. Restarting...")
                agent_process = subprocess.Popen([sys.executable, agent_path, "start"], cwd=base_dir)
            if server_process.poll() is not None:
                print("⚠️ Server process stopped. Restarting...")
                server_process = subprocess.Popen([sys.executable, server_path], cwd=base_dir)
    except KeyboardInterrupt:
        print("🛑 Stopping all processes...")
        agent_process.terminate()
        server_process.terminate()

if __name__ == "__main__":
    run()
