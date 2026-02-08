# Python environment setup

1. Ensure the virtual environment exists:
   ```bash
   python -m venv .venv
   ```
2. Activate the environment for every Python-related command:
   ```bash
   source .venv/bin/activate
   ```
3. Install dependencies before running tests:
   ```bash
   pip install -r requirements.txt
   ```
4. Export the required environment variables (or copy `.env.test`):
   ```bash
   cp .env.test .env
   ```
5. Run Python checks/commands only after the environment is active and required variables are loaded.
