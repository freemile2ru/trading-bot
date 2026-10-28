#!/bin/bash
set -e

echo "=== Crypto Trading Oracle ==="

# Backend
cd backend
if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "Created .env from .env.example — add your API keys!"
fi

if [ ! -d "venv" ]; then
  echo "Creating Python venv with Python 3.12..."
  /usr/local/bin/python3.12 -m venv venv
fi

source venv/bin/activate
pip install -q -r requirements.txt

echo "Starting backend on http://localhost:8000 ..."
python main.py &
BACKEND_PID=$!

# Keep the Mac awake while the backend runs: a sleeping Mac gets no prices, so stops aren't
# managed and the experiments lose days (2026-10-14..17). -i: no idle sleep; -s: no system
# sleep on mains power. Closing the lid on battery still sleeps it. Ends with the backend.
if command -v caffeinate >/dev/null 2>&1; then
  caffeinate -i -s -w $BACKEND_PID &
  echo "Keeping the Mac awake while the backend runs (caffeinate)."
fi

# Frontend
cd ../frontend
if [ ! -d "node_modules" ]; then
  echo "Installing frontend deps..."
  npm install
fi

echo "Starting frontend on http://localhost:3000 ..."
npm run dev &
FRONTEND_PID=$!

echo ""
echo "Backend:  http://localhost:8000"
echo "Frontend: http://localhost:3000"
echo "API docs: http://localhost:8000/docs"
echo ""
echo "Press Ctrl+C to stop both servers."

trap "kill $BACKEND_PID $FRONTEND_PID 2>/dev/null; exit" INT TERM
wait
