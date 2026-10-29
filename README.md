# Pizza Studio Club

The [live FastAPI demo](https://pizza-studio-club-demo-k7m4q.onrender.com/) is deployed on Render. The [GitHub Pages page](https://karthik220302.github.io/pizza-studio-club-k7m4q/) redirects there. `preview.html` retains the original static concept preview.

## Proposed flexible private rooms

This university business concept proposes six private rooms. Each individual room accommodates 1–8 people. A movable partition could join rooms 1 + 2 or 3 + 4 into one shared dining space for 9–16 people; rooms 5 and 6 remain individual. The partitions are **proposed**, not installed. Combined bookings reserve both physical rooms for one 90-minute session. Sessions start every 30 minutes from 10:00 through 19:30, closing by 21:00. The availability API returns only room types that fit the guest count and offers later dates when none is available.

## Pricing and booking flow

New bookings use one €22 Duo Package per two joined guests, rounded up for odd groups. Each package includes two pizzas and two drinks; additional menu items are charged separately. A package costs €14 when either member of that pair redeems a valid 10-stamp reward. A reward code can be used only once. An optional extra-food package costs €12; its contents are still a concept placeholder and should be defined before real sales. There is no room supplement or large-group fee. The organizer can invite up to 15 others, choose individual, equal, custom or one-person bill splitting, and use simulated payments. Fully paid bookings receive signed QR check-in and PDF invoices and receipts.

Staff issue one-use demo loyalty codes in the **Staff** view with the configured `STAFF_PIN`. Codes are stored as hashes and atomically marked used when the eligible guest creates or joins a booking. Pairing follows participant join order. Existing bookings retain their earlier pricing through the `pricing_version=1` migration default; newly created bookings use version 2.

## Run and test

From `backend/`, install `requirements.txt`, then run `uvicorn app.server:app --host 127.0.0.1 --port 8000`. Set `DEMO_MODE=true`, `SECRET_KEY`, and `STAFF_PIN` for a local demo. Run `PYTHONPATH=. python3 tests/test_flexible_rooms.py` to exercise group sizes, conflicts, simultaneous checkout, pricing, splits, loyalty, QR and PDF.

`render.yaml` deploys the backend on Render's free Python plan with generated secrets. SQLite storage on that plan is temporary: bookings and reward codes can disappear on a restart or redeploy. Payments remain simulated; no real card is charged. This public classroom demo is not a production reservation service.
