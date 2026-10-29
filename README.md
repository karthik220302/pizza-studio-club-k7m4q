# Pizza Studio Club

The GitHub Pages home page redirects to the live FastAPI demo on Render. The original static concept preview remains at `preview.html`. The `backend/` folder contains the FastAPI app with bookings, group orders, simulated payments, invitation links, QR check-in, and PDF receipts.

The `render.yaml` Blueprint deploys the backend on Render's free Python plan. It generates separate server and staff secrets. The backend uses temporary SQLite storage on that plan, so bookings can disappear when the service restarts or redeploys. It is a public classroom demo, not a reservation system for real customers. Payments are simulated and do not charge a card.

Run the backend locally from `backend/` with `pip install -r requirements.txt` and `uvicorn app.server:app --host 127.0.0.1 --port 8000`. The public deployment reads its base URL from Render's `RENDER_EXTERNAL_URL` variable.
