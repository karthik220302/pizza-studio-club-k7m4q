"""Pizza Studio Club | integrated room, menu, split billing and PDF demo.

Run with: python -m uvicorn app.server:app --reload
Set DEMO_MODE=false, SECRET_KEY, STAFF_PIN and STRIPE_SECRET_KEY for payment integration.
No card data are collected or stored by this application.
"""
from __future__ import annotations
import base64, datetime as dt, hashlib, hmac, io, json, os, re, secrets, shutil, sqlite3, time, urllib.parse
from contextlib import contextmanager
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo
import qrcode
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor
from reportlab.lib.utils import ImageReader
import requests

ROOT=Path(__file__).resolve().parent
DB=Path(os.environ.get('PIZZA_DB',str(ROOT.parent/'restaurant.sqlite3')))
DEMO=os.environ.get('DEMO_MODE','true').lower()=='true'
SECRET=os.environ.get('SECRET_KEY','demo-only-key-please-change-before-deployment')
STAFF_PIN=os.environ.get('STAFF_PIN','' if os.environ.get('RENDER') else ('2468' if DEMO else ''))
STRIPE_KEY=os.environ.get('STRIPE_SECRET_KEY','')
STRIPE_WEBHOOK=os.environ.get('STRIPE_WEBHOOK_SECRET','')
ORIGIN=(os.environ.get('PUBLIC_URL') or os.environ.get('RENDER_EXTERNAL_URL') or 'http://127.0.0.1:8000').rstrip('/')
STUDIO_NAME=os.environ.get('RESTAURANT_NAME','Pizza Studio Club')
STUDIO_ADDRESS=os.environ.get('RESTAURANT_ADDRESS','Paris, France • Demonstration restaurant')
BUSINESS_NUMBER=os.environ.get('BUSINESS_NUMBER','NOT CONFIGURED')
VAT_PCT=int(os.environ.get('VAT_PERCENT','10'))
SESSION_MINS=90
BASE=2200
LOYALTY=1400
ROOM_FEE=1200
CAPACITY=8
ROOM_COUNT=6
HOLD_MINS=45
MENU=[
  ('pizza_margherita','Margherita Pizza','Pizza','Tomato, mozzarella & basil',1190,'🍕'),
  ('pizza_pepperoni','Pepperoni Pizza','Pizza','Pepperoni & melted cheese',1390,'🍕'),
  ('pizza_veggie','Garden Veggie Pizza','Pizza','Peppers, mushrooms & olives',1290,'🥬'),
  ('pizza_bbq','Smoky BBQ Chicken','Pizza','Chicken & smoky barbecue',1490,'🍕'),
  ('burger_classic','Classic Smash Burger','Burgers','Grilled beef, cheese & sauce',990,'🍔'),
  ('burger_veggie','Crispy Veggie Burger','Burgers','Plant-based patty & salad',990,'🍔'),
  ('fries','Golden Fries','Sides','Crispy shoestring fries',390,'🍟'),
  ('loaded_fries','Loaded Cheese Fries','Sides','Cheese sauce & herbs',590,'🍟'),
  ('nuggets','Chicken Bites','Sides','Six golden chicken pieces',690,'🍗'),
  ('cola','Cola','Drinks','Chilled 330 ml',250,'🥤'),
  ('water','Still Water','Drinks','Still water 500 ml',190,'💧'),
  ('lemonade','Sparkling Lemonade','Drinks','House citrus lemonade',290,'🍋'),
  ('cookie','Chocolate Cookie','Desserts','Warm chocolate cookie',290,'🍪'),
  ('brownie','Fudge Brownie','Desserts','Chocolate brownie',390,'🍫')
]
MENUD={x[0]:{'id':x[0],'name':x[1],'category':x[2],'description':x[3],'price_cents':x[4],'emoji':x[5]} for x in MENU}
app=FastAPI(title='Pizza Studio Club — Integrated Restaurant Prototype',version='1.0')
app.mount('/static',StaticFiles(directory=str(ROOT/'static')),name='static')

if os.environ.get('RENDER') and SECRET=='demo-only-key-please-change-before-deployment':
 raise RuntimeError('SECRET_KEY must be configured for a public deployment.')

def utc():return dt.datetime.now(dt.timezone.utc)
def stamp():return utc().isoformat()
def hsh(s):return hashlib.sha256(s.encode()).hexdigest()
def uid(prefix):return prefix+'-'+secrets.token_hex(5).upper()
def fail(status,msg):raise HTTPException(status_code=status,detail=msg)

def conn():
 c=sqlite3.connect(DB,timeout=12,isolation_level=None)
 c.row_factory=sqlite3.Row;c.execute('PRAGMA foreign_keys=ON');c.execute('PRAGMA busy_timeout=12000');return c
@contextmanager
def transaction():
 c=conn()
 try:
  c.execute('BEGIN IMMEDIATE');yield c;c.commit()
 except Exception:c.rollback();raise
 finally:c.close()

def init_db():
 DB.parent.mkdir(parents=True,exist_ok=True)
 with conn() as c:
  c.execute('PRAGMA journal_mode=WAL')
  c.executescript('''
 CREATE TABLE IF NOT EXISTS bookings(id TEXT PRIMARY KEY, date TEXT NOT NULL, start TEXT NOT NULL,end TEXT NOT NULL,
 requested_room INTEGER, room INTEGER,status TEXT NOT NULL DEFAULT 'planning',hold_until TEXT,
 organizer_id TEXT,invite_hash TEXT NOT NULL,split_mode TEXT NOT NULL DEFAULT 'individual',
 loyal_organizer INTEGER NOT NULL DEFAULT 0,locked INTEGER NOT NULL DEFAULT 0,expected_guests INTEGER NOT NULL DEFAULT 8,created_at TEXT NOT NULL, confirmed_at TEXT);
 CREATE TABLE IF NOT EXISTS members(id TEXT PRIMARY KEY,booking_id TEXT NOT NULL,name TEXT NOT NULL,email TEXT,auth_hash TEXT NOT NULL,
 organizer INTEGER NOT NULL DEFAULT 0,loyal INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,
 FOREIGN KEY(booking_id) REFERENCES bookings(id) ON DELETE CASCADE);
 CREATE TABLE IF NOT EXISTS order_items(id TEXT PRIMARY KEY,booking_id TEXT NOT NULL,member_id TEXT,product_id TEXT NOT NULL,
 qty INTEGER NOT NULL,unit_cents INTEGER NOT NULL,shared INTEGER NOT NULL DEFAULT 0,
 FOREIGN KEY(booking_id) REFERENCES bookings(id) ON DELETE CASCADE);
 CREATE TABLE IF NOT EXISTS charges(id TEXT PRIMARY KEY,booking_id TEXT NOT NULL,member_id TEXT NOT NULL,
 amount_cents INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'unpaid',provider_session TEXT,
 paid_at TEXT,method TEXT, UNIQUE(booking_id,member_id), FOREIGN KEY(booking_id) REFERENCES bookings(id) ON DELETE CASCADE);
 CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,created_at TEXT NOT NULL);
 CREATE INDEX IF NOT EXISTS idx_rooms ON bookings(date,start,end,room,status);
 CREATE INDEX IF NOT EXISTS idx_members_booking ON members(booking_id);
 ''')
  # Retain existing bookings when upgrading a previously installed demo database.
  cols={row['name'] for row in c.execute('PRAGMA table_info(bookings)')}
  if 'expected_guests' not in cols:
   c.execute('ALTER TABLE bookings ADD COLUMN expected_guests INTEGER NOT NULL DEFAULT 8')
init_db()

class NewBooking(BaseModel):
 date:str;time:str;name:str=Field(min_length=1,max_length=80);email:str='';requested_room:Optional[int]=Field(None,ge=1,le=6);loyal:bool=False;expected_guests:int=Field(2,ge=1,le=8)
class Join(BaseModel):
 invite:str;name:str=Field(min_length=1,max_length=80);email:str='';loyal:bool=False
class AddItem(BaseModel):
 product_id:str;qty:int=Field(1,ge=1,le=20);shared:bool=False
class UpdateQty(BaseModel):qty:int=Field(ge=0,le=20)
class Split(BaseModel):mode:str;custom:dict[str,int]={};payer_id:Optional[str]=None
class Room(BaseModel):room:Optional[int]=Field(None,ge=1,le=6)
class GuestPlan(BaseModel):expected_guests:int=Field(ge=1,le=8)
class Payment(BaseModel):idempotency_key:str=Field(min_length=3,max_length=100)
class StaffVerification(BaseModel):token:str

def parse_date_time(d,t):
 try:
  day=dt.date.fromisoformat(d);moment=dt.datetime.strptime(t,'%H:%M').time()
 except ValueError:fail(422,'Choose a valid date and time.')
 if day < dt.date.today():fail(422,'The booking date must not be in the past.')
 if moment.minute%30!=0 or moment.second!=0:fail(422,'Slots start every 30 minutes.')
 begin=dt.datetime.combine(day,moment)
 if begin.time()<dt.time(10,0) or (begin+dt.timedelta(minutes=SESSION_MINS)).time()>dt.time(21,0):fail(422,'Choose a 90-minute slot between 10:00 and 21:00 (last start 19:30).')
 return begin,(begin+dt.timedelta(minutes=SESSION_MINS)).strftime('%H:%M')

def available(c,date,start,end,exclude=None):
 # An incomplete group holds its assigned room only while the hold is valid.
 rows=c.execute('''SELECT room FROM bookings WHERE date=? AND room IS NOT NULL AND start < ? AND end > ?
 AND (status='confirmed' OR (status='held' AND hold_until > ?)) AND id<>?''',
 (date,end,start,stamp(),exclude or '')).fetchall()
 taken={r['room'] for r in rows}
 return [r for r in range(1,ROOM_COUNT+1) if r not in taken]

def auth(bid, authorization):
 if not authorization or not authorization.startswith('Bearer '):fail(401,'You must open the booking using your private participant access link.')
 token=authorization.removeprefix('Bearer ').strip()
 with conn() as c:
  person=c.execute('SELECT * FROM members WHERE booking_id=? AND auth_hash=?',(bid,hsh(token))).fetchone()
 if not person:fail(403,'Access denied for this booking.')
 return dict(person)

def booking(c,bid):
 row=c.execute('SELECT * FROM bookings WHERE id=?',(bid,)).fetchone()
 if not row:fail(404,'Booking not found.')
 return row

def organizer(member):
 if not member['organizer']:fail(403,'Only the booking organizer can make this change.')

def can_edit(b):
 if b['locked']:fail(409,'This order is locked for payment. Create a new booking to make changes.')

def rows_for(c,bid):
 m=[dict(r) for r in c.execute('SELECT id,name,email,organizer,loyal FROM members WHERE booking_id=? ORDER BY created_at,id',(bid,))]
 order=[dict(r) for r in c.execute('SELECT * FROM order_items WHERE booking_id=?',(bid,))]
 for r in order:r['product']=MENUD.get(r['product_id'],{'name':r['product_id'],'category':'Other','price_cents':r['unit_cents']})
 charges=[dict(r) for r in c.execute('SELECT id,member_id,amount_cents,status,paid_at,method FROM charges WHERE booking_id=?',(bid,))]
 return m,order,charges

def calc_bill(b, members, items):
 n=len(members)
 if not n:fail(409,'At least one guest is required.')
 # Each participant books a studio seat and receives a personal loyalty saving.
 seat={m['id']:(LOYALTY if m['loyal'] else BASE) for m in members}
 seat_total=sum(seat.values())
 foods=sum(i['unit_cents']*i['qty'] for i in items)
 # Organizer passport waives private-room supplement for the entire group.
 room_charge=0 if b['loyal_organizer'] else ROOM_FEE
 total=seat_total+foods+room_charge
 tax=round(total*VAT_PCT/(100+VAT_PCT))  # indicative, VAT-inclusive prices
 return {'seat_total_cents':seat_total,'food_cents':foods,'room_fee_cents':room_charge,
 'total_cents':total,'tax_cents':tax,'before_tax_cents':total-tax,
 'loyalty_savings_cents':sum(BASE-v for v in seat.values())+(ROOM_FEE if b['loyal_organizer'] else 0)}

def distribute(total,ids):
 ans={i:total//len(ids) for i in ids}
 for i in ids[:total%len(ids)]:ans[i]+=1
 return ans

def allocations(b, members, items, bill, split):
 ids=[m['id'] for m in members];n=len(ids)
 mode=split.mode
 if mode not in ('individual','equal','custom','one'):fail(422,'Invalid payment split mode.')
 if mode=='equal':return distribute(bill['total_cents'],ids)
 if mode=='one':
  payer=split.payer_id or b['organizer_id']
  if payer not in ids:fail(422,'Payer must be in the booking.')
  return {i:bill['total_cents'] if i==payer else 0 for i in ids}
 if mode=='custom':
  if set(split.custom)!=set(ids):fail(422,'Add an amount for every invited guest.')
  if any(not isinstance(a,int) or a<0 for a in split.custom.values()):fail(422,'Amounts must be nonnegative whole cents.')
  if sum(split.custom.values())!=bill['total_cents']:fail(422,'Custom split amounts must exactly equal the total.')
  return split.custom
 amount={m['id']:LOYALTY if m['loyal'] else BASE for m in members}
 fee=distribute(bill['room_fee_cents'],ids)
 for i in ids:amount[i]+=fee[i]
 for item in items:
  sub=item['qty']*item['unit_cents']
  if item['shared']:
   shares=distribute(sub,ids)
   for i in ids:amount[i]+=shares[i]
  else:
   if item['member_id'] not in ids:fail(422,'One menu item has an invalid owner.')
   amount[item['member_id']]+=sub
 if sum(amount.values())!=bill['total_cents']:fail(500,'Bill allocation did not balance.')
 return amount

def public_view(c,bid):
 b=dict(booking(c,bid));m,order,ch=rows_for(c,bid);bill=calc_bill(b,m,order)
 # Emails not returned in shared group view; just member first names.
 for x in m:x.pop('email',None)
 return {'booking':{k:v for k,v in b.items() if k!='invite_hash'},'members':m,'items':order,'charges':ch,'bill':bill,'demo_mode':DEMO}

@app.get('/')
def index():return FileResponse(ROOT/'static'/'index.html')
@app.get('/health')
def health():
 with conn() as c:c.execute('SELECT 1').fetchone()
 return {'status':'ok'}
@app.get('/api/config')
def config():return {'demo_mode':DEMO,'restaurant':STUDIO_NAME,'hours':'10:00–21:00','duration_minutes':SESSION_MINS,'room_count':ROOM_COUNT,'capacity':CAPACITY,'menu':list(MENUD.values()),'seat_regular_cents':BASE,'seat_loyal_cents':LOYALTY,'room_fee_cents':ROOM_FEE,'tax_percent_illustrative':VAT_PCT,'stripe_enabled':bool(STRIPE_KEY)}
@app.get('/api/availability')
def slots(date:str,requested_room:Optional[int]=None):
 try:dt.date.fromisoformat(date)
 except:fail(422,'Invalid date.')
 data=[]
 with conn() as c:
  for hh in range(10,20):
   for minute in (0,30):
    t=f'{hh:02d}:{minute:02d}'
    try:_,end=parse_date_time(date,t)
    except HTTPException:continue
    rooms=available(c,date,t,end)
    if requested_room:rooms=[r for r in rooms if r==requested_room]
    if rooms:data.append({'start':t,'end':end,'free_rooms':len(rooms),'rooms':rooms})
 return {'date':date,'slots':data}
@app.post('/api/bookings')
def create(req:NewBooking):
 _,end=parse_date_time(req.date,req.time)
 if req.loyal and not DEMO:fail(403,'A loyalty reward requires verified stamps from a connected loyalty account.')
 if req.email and ('@' not in req.email or len(req.email)>150):fail(422,'Please enter a valid email.')
 bid=uid('PSC');member_id=uid('G');token=secrets.token_urlsafe(32);invite=secrets.token_urlsafe(32)
 with transaction() as c:
  c.execute('INSERT INTO bookings(id,date,start,end,requested_room,status,invite_hash,organizer_id,loyal_organizer,expected_guests,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
 (bid,req.date,req.time,end,req.requested_room,'planning',hsh(invite),member_id,int(req.loyal),req.expected_guests,stamp()))
  c.execute('INSERT INTO members(id,booking_id,name,email,auth_hash,organizer,loyal,created_at) VALUES (?,?,?,?,?,?,?,?)',
 (member_id,bid,req.name.strip(),req.email.strip(),hsh(token),1,int(req.loyal),stamp()))
 return {'booking_id':bid,'member_id':member_id,'member_token':token,'invite_url':f'{ORIGIN}/?invite={urllib.parse.quote(invite)}&booking={urllib.parse.quote(bid)}','invite_token':invite,'booking':{'date':req.date,'time':req.time,'end':end}}
@app.post('/api/bookings/{bid}/join')
def join(bid:str,req:Join):
 if req.loyal and not DEMO:fail(403,'A loyalty reward requires verified stamps from a connected loyalty account.')
 token=secrets.token_urlsafe(32);member_id=uid('G')
 with transaction() as c:
  b=booking(c,bid);can_edit(b)
  if not hmac.compare_digest(b['invite_hash'],hsh(req.invite)):fail(403,'This invitation link is invalid.')
  count=c.execute('SELECT COUNT(*) c FROM members WHERE booking_id=?',(bid,)).fetchone()['c']
  if count>=min(CAPACITY,b['expected_guests']):fail(409,'Your planned guest count is full. Ask the organizer to increase the number of guests in My group before inviting more people.')
  c.execute('INSERT INTO members(id,booking_id,name,email,auth_hash,organizer,loyal,created_at) VALUES (?,?,?,?,?,?,?,?)',
 (member_id,bid,req.name.strip(),req.email,hsh(token),0,int(req.loyal),stamp()))
 return {'booking_id':bid,'member_id':member_id,'member_token':token}
@app.get('/api/bookings/{bid}')
def get_book(bid:str,authorization:Optional[str]=Header(None)):
 auth(bid,authorization)
 with conn() as c:return public_view(c,bid)
@app.delete('/api/bookings/{bid}')
def delete_past_booking(bid:str,authorization:Optional[str]=Header(None)):
 who=auth(bid,authorization);organizer(who)
 with transaction() as c:
  b=booking(c,bid)
  end=dt.datetime.fromisoformat(f"{b['date']}T{b['end']}").replace(tzinfo=ZoneInfo('Europe/Paris'))
  if end>=dt.datetime.now(ZoneInfo('Europe/Paris')):
   fail(409,'This booking can be deleted after its session has ended.')
  c.execute('DELETE FROM bookings WHERE id=?',(bid,))
 if re.fullmatch(r'PSC-[A-F0-9]{10}',bid):
  shutil.rmtree(ROOT.parent/'private_invoices'/bid,ignore_errors=True)
 return {'deleted':True,'booking_id':bid}
@app.post('/api/bookings/{bid}/guest-count')
def set_guest_count(bid:str,req:GuestPlan,authorization:Optional[str]=Header(None)):
 who=auth(bid,authorization);organizer(who)
 with transaction() as c:
  b=booking(c,bid);can_edit(b)
  count=c.execute('SELECT COUNT(*) c FROM members WHERE booking_id=?',(bid,)).fetchone()['c']
  if req.expected_guests<count:fail(422,'The planned guest count cannot be smaller than the number already joined.')
  c.execute('UPDATE bookings SET expected_guests=? WHERE id=?',(req.expected_guests,bid))
  return public_view(c,bid)
@app.post('/api/bookings/{bid}/room')
def set_room(bid:str,req:Room,authorization:Optional[str]=Header(None)):
 person=auth(bid,authorization);organizer(person)
 with transaction() as c:
  b=booking(c,bid);can_edit(b)
  c.execute('UPDATE bookings SET requested_room=? WHERE id=?',(req.room,bid))
  return public_view(c,bid)
@app.post('/api/bookings/{bid}/items')
def add_item(bid:str,req:AddItem,authorization:Optional[str]=Header(None)):
 who=auth(bid,authorization)
 if req.product_id not in MENUD:fail(422,'Menu item not found.')
 with transaction() as c:
  b=booking(c,bid);can_edit(b)
  c.execute('INSERT INTO order_items(id,booking_id,member_id,product_id,qty,unit_cents,shared) VALUES (?,?,?,?,?,?,?)',
 (uid('I'),bid,who['id'],req.product_id,req.qty,MENUD[req.product_id]['price_cents'],int(req.shared)))
  return public_view(c,bid)
@app.patch('/api/bookings/{bid}/items/{iid}')
def change_item(bid:str,iid:str,req:UpdateQty,authorization:Optional[str]=Header(None)):
 who=auth(bid,authorization)
 with transaction() as c:
  b=booking(c,bid);can_edit(b)
  item=c.execute('SELECT * FROM order_items WHERE id=? AND booking_id=?',(iid,bid)).fetchone()
  if not item:fail(404,'Item not found.')
  if item['member_id']!=who['id'] and not who['organizer']:fail(403,'You can only change your own order.')
  if req.qty==0:c.execute('DELETE FROM order_items WHERE id=?',(iid,))
  else:c.execute('UPDATE order_items SET qty=? WHERE id=?',(req.qty,iid))
  return public_view(c,bid)
@app.delete('/api/bookings/{bid}/members/{mid}')
def remove_member(bid:str,mid:str,authorization:Optional[str]=Header(None)):
 who=auth(bid,authorization);organizer(who)
 with transaction() as c:
  b=booking(c,bid);can_edit(b)
  if mid==who['id']:fail(422,'The organizer cannot remove themself.')
  c.execute('DELETE FROM order_items WHERE booking_id=? AND member_id=?',(bid,mid))
  c.execute('DELETE FROM members WHERE id=? AND booking_id=?',(mid,bid))
  return public_view(c,bid)
@app.post('/api/bookings/{bid}/checkout')
def checkout(bid:str,req:Split,authorization:Optional[str]=Header(None)):
 who=auth(bid,authorization);organizer(who)
 with transaction() as c:
  b=booking(c,bid);can_edit(b)
  members,items,_=rows_for(c,bid)
  bill=calc_bill(b,members,items);alloc=allocations(b,members,items,bill,req)
  free=available(c,b['date'],b['start'],b['end'],exclude=bid)
  room=b['requested_room'] or (free[0] if free else None)
  if room not in free:fail(409,'Selected room is unavailable. Please choose another room or time.')
  hold=(utc()+dt.timedelta(minutes=HOLD_MINS)).isoformat()
  c.execute("UPDATE bookings SET locked=1,status='held',room=?,hold_until=?,split_mode=? WHERE id=?",
 (room,hold,req.mode,bid))
  for mid,amount in alloc.items():
   c.execute('INSERT INTO charges(id,booking_id,member_id,amount_cents,status) VALUES (?,?,?,?,?)',
 (uid('PAY'),bid,mid,amount,'unpaid' if amount>0 else 'paid'))
  if sum(alloc.values())!=bill['total_cents']:fail(500,'Split mismatch.')
  return public_view(c,bid)
@app.post('/api/bookings/{bid}/pay/demo')
def demo_pay(bid:str,req:Payment,authorization:Optional[str]=Header(None)):
 if not DEMO:fail(404,'Demo payments are disabled on this server.')
 who=auth(bid,authorization)
 with transaction() as c:
  b=booking(c,bid)
  if b['status'] not in ('held','confirmed'):fail(409,'Finalize the group order before paying.')
  if b['status']=='held' and b['hold_until']<stamp():fail(409,'This room hold expired. Contact staff before paying.')
  charge=c.execute('SELECT * FROM charges WHERE booking_id=? AND member_id=?',(bid,who['id'])).fetchone()
  if not charge:fail(404,'Your balance has not been created.')
  if charge['status']=='paid':return public_view(c,bid) # idempotent
  c.execute("UPDATE charges SET status='paid',method='demo_simulation',paid_at=? WHERE id=?",(stamp(),charge['id']))
  finish_if_fully_paid(c,bid)
  view=public_view(c,bid)
 if view['booking']['status']=='confirmed':materialize_documents(bid)
 return view

def finish_if_fully_paid(c,bid):
 remain=c.execute("SELECT COUNT(*) n FROM charges WHERE booking_id=? AND status!='paid'",(bid,)).fetchone()['n']
 if remain==0:
  c.execute("UPDATE bookings SET status='confirmed',confirmed_at=?,hold_until=NULL WHERE id=?",(stamp(),bid))

@app.post('/api/bookings/{bid}/checkout/stripe')
def stripe_checkout(bid:str,req:Payment,authorization:Optional[str]=Header(None)):
 if not STRIPE_KEY:fail(503,'Live payments are not configured. Use the labeled demo checkout instead.')
 who=auth(bid,authorization)
 with transaction() as c:
  b=booking(c,bid)
  if b['status']!='held':fail(409,'Booking must have an active hold.')
  if b['hold_until']<stamp():fail(409,'Room hold has expired.')
  charge=c.execute('SELECT * FROM charges WHERE booking_id=? AND member_id=?',(bid,who['id'])).fetchone()
  if not charge:fail(404,'Charge not found.')
  if charge['status']=='paid':return {'already_paid':True}
  if not charge['amount_cents']:return {'already_paid':True}
  if (dt.datetime.fromisoformat(b['hold_until'])-utc()).total_seconds()<1805:
   fail(409,'Less than 30 minutes remain on the room hold. Please contact the organizer to renew your booking.')
  if charge['provider_session']:
   ref=charge['provider_session']
   remote=requests.get('https://api.stripe.com/v1/checkout/sessions/'+ref,auth=(STRIPE_KEY,''),timeout=15)
   if remote.ok and remote.json().get('status')=='open':return {'url':remote.json()['url']}
  form={'mode':'payment','success_url':f'{ORIGIN}/?booking={bid}&payment=return',
  'cancel_url':f'{ORIGIN}/?booking={bid}&payment=cancel',
  'client_reference_id':charge['id'],'metadata[booking_id]':bid,'metadata[member_id]':who['id'],'metadata[charge_id]':charge['id'],
  'line_items[0][price_data][currency]':'eur','line_items[0][price_data][unit_amount]':charge['amount_cents'],
  'line_items[0][price_data][product_data][name]':f'Pizza Studio Club booking {bid}',
  'line_items[0][quantity]':1,'expires_at':int(time.time())+1800}
  resp=requests.post('https://api.stripe.com/v1/checkout/sessions',auth=(STRIPE_KEY,''),data=form,
     headers={'Idempotency-Key':f'psc-{charge["id"]}-{req.idempotency_key}'},timeout=20)
  if not resp.ok:fail(502,'Payment provider could not create checkout: '+resp.text[:180])
  session=resp.json()
  c.execute('UPDATE charges SET provider_session=? WHERE id=?',(session['id'],charge['id']))
  return {'url':session['url']}

def verify_stripe(payload:bytes,header:str):
 if not STRIPE_WEBHOOK:fail(503,'Webhook signing secret missing.')
 parts=dict(x.split('=',1) for x in header.split(',') if '=' in x)
 try:ts=int(parts['t']);sig=parts['v1']
 except:fail(400,'Missing Stripe signature.')
 if abs(time.time()-ts)>300:fail(400,'Webhook timestamp is too old.')
 expected=hmac.new(STRIPE_WEBHOOK.encode(),(str(ts)+'.').encode()+payload,hashlib.sha256).hexdigest()
 if not hmac.compare_digest(expected,sig):fail(400,'Invalid payment webhook signature.')
@app.post('/api/payments/stripe/webhook')
async def stripe_webhook(request:Request,stripe_signature:Optional[str]=Header(None)):
 raw=await request.body();verify_stripe(raw,stripe_signature or '')
 event=json.loads(raw)
 if event.get('type')!='checkout.session.completed':return {'received':True}
 obj=event['data']['object']
 if obj.get('payment_status')!='paid':return {'received':True}
 eid=event['id'];cid=obj.get('client_reference_id');bid=obj.get('metadata',{}).get('booking_id')
 if not cid or not bid:fail(400,'Missing payment metadata.')
 with transaction() as c:
  if c.execute('SELECT 1 FROM events WHERE id=?',(eid,)).fetchone():return {'received':True}
  charge=c.execute('SELECT * FROM charges WHERE id=? AND booking_id=?',(cid,bid)).fetchone()
  if not charge:fail(404,'Charge not found.')
  if charge['amount_cents']!=obj.get('amount_total') or obj.get('currency')!='eur' or charge['provider_session']!=obj.get('id'):
   fail(400,'Payment amount, currency or session mismatch.')
  c.execute('INSERT INTO events(id,created_at) VALUES (?,?)',(eid,stamp()))
  booking_row=booking(c,bid)
  if booking_row['status']=='held' and booking_row['hold_until']<stamp():
   # Do not confirm expired inventory just because a payment eventually settled.
   c.execute("UPDATE charges SET status='paid_review',method='stripe',paid_at=? WHERE id=?",(stamp(),cid))
  else:
   c.execute("UPDATE charges SET status='paid',method='stripe',paid_at=? WHERE id=?",(stamp(),cid))
   finish_if_fully_paid(c,bid)
 with conn() as c:
  if booking(c,bid)['status']=='confirmed':materialize_documents(bid)
 return {'received':True}

# Authenticated booking QR: holds a signed reference, not a person or payment secret.
def qr_token(b):
 value={'bid':b['id'],'room':b['room'],'date':b['date'],'start':b['start'],'end':b['end']}
 raw=json.dumps(value,separators=(',',':')).encode()
 part=base64.urlsafe_b64encode(raw).rstrip(b'=').decode();sig=hmac.new(SECRET.encode(),part.encode(),hashlib.sha256).hexdigest()
 return part+'.'+sig

def verify_qr(token):
 try:part,signature=token.split('.',1)
 except:fail(400,'Invalid QR token.')
 expected=hmac.new(SECRET.encode(),part.encode(),hashlib.sha256).hexdigest()
 if not hmac.compare_digest(expected,signature):fail(403,'The QR code signature is invalid.')
 try:payload=json.loads(base64.urlsafe_b64decode(part+'='*(-len(part)%4)))
 except:fail(400,'Unreadable QR token.')
 with conn() as c:
  b=booking(c,payload['bid'])
 if b['status']!='confirmed':fail(409,'Booking has not been fully paid.')
 for k,v in [('room','room'),('date','date'),('start','start'),('end','end')]:
  if payload.get(k)!=b[v]:fail(409,'Booking QR details do not match the reservation.')
 return {'valid':True,'booking_id':b['id'],'room':b['room'],'date':b['date'],'time':b['start'],'end':b['end'],'status':b['status']}

def qrcode_bytes(text):
 img=qrcode.QRCode(version=None,error_correction=qrcode.constants.ERROR_CORRECT_M,box_size=8,border=2);img.add_data(text);img.make(fit=True)
 buf=io.BytesIO();img.make_image(fill_color='#203d31',back_color='white').convert('RGB').save(buf,format='PNG');return buf.getvalue()
@app.get('/api/bookings/{bid}/qr')
def qrcode_png(bid:str,authorization:Optional[str]=Header(None)):
 auth(bid,authorization)
 with conn() as c:b=booking(c,bid)
 if b['status']!='confirmed':fail(409,'QR issued only after all required payments are complete.')
 return Response(content=qrcode_bytes(f'{ORIGIN}/checkin?token={urllib.parse.quote(qr_token(b))}'),media_type='image/png')
@app.get('/checkin',response_class=HTMLResponse)
def checkin_page():return FileResponse(ROOT/'static'/'staff.html')
@app.post('/api/staff/verify')
def staff_verify(req:StaffVerification,x_staff_pin:Optional[str]=Header(None)):
 if not STAFF_PIN or not hmac.compare_digest(x_staff_pin or '',STAFF_PIN):fail(403,'Staff verification PIN required.')
 return verify_qr(req.token)

# Reportlab A4 invoice/receipt builder. Demo payments are always clearly watermarked.
def pdf_invoice(bid, mid=None):
 with conn() as c:
  b=dict(booking(c,bid));members,items,charges=rows_for(c,bid)
 if b['status']!='confirmed':fail(409,'Invoice PDFs are issued once the booking is fully paid.')
 bill=calc_bill(b,members,items)
 payer=next((m for m in members if m['id']==mid),None) if mid else None
 charge=next((p for p in charges if p['member_id']==mid),None) if mid else None
 if mid and (not payer or not charge or charge['status']!='paid'):fail(404,'Paid member receipt not found.')
 name='Member payment receipt' if mid else 'Consolidated booking invoice'
 qr_data=qrcode_bytes(f'{ORIGIN}/checkin?token={urllib.parse.quote(qr_token(b))}')
 buff=io.BytesIO();cv=canvas.Canvas(buff,pagesize=(595.27,841.89))
 W,H=595.27,841.89
 cv.setFillColor(HexColor('#21382d'));cv.rect(0,H-136,W,136,fill=1,stroke=0)
 cv.setFillColor(HexColor('#ffcb89'));cv.setFont('Helvetica-Bold',17);cv.drawString(42,H-47,'PIZZA  /  STUDIO CLUB')
 cv.setFillColor(HexColor('#ffffff'));cv.setFont('Helvetica',10);cv.drawString(42,H-68,'PIZZA  •  BURGERS  •  GOOD TIMES')
 cv.setFont('Helvetica-Bold',13);cv.drawString(42,H-105,name.upper())
 y=H-166
 def label(key,value):
  nonlocal y
  cv.setFont('Helvetica-Bold',9);cv.setFillColor(HexColor('#627068'));cv.drawString(43,y,key)
  cv.setFont('Helvetica',9.5);cv.setFillColor(HexColor('#263a31'));cv.drawString(185,y,str(value)[:58]);y-=19
 label('Restaurant',STUDIO_NAME)
 label('Address',STUDIO_ADDRESS)
 label('Booking ID',bid)
 label('Invoice number',('RCPT-' if mid else 'INV-')+bid+(('-'+mid[-5:]) if mid else ''))
 label('Issued at',utc().strftime('%d %b %Y %H:%M UTC'))
 label('Session',f"{b['date']}  {b['start']} - {b['end']}")
 label('Room',f"ROOM {b['room']:02d}  /  PAID & CONFIRMED")
 if payer:
  label('Guest',payer['name']);label('Guest email',payer['email'] or 'Not provided')
 else:
  lead=next((x for x in members if x['organizer']),members[0])
  label('Organizer',lead['name']);label('Contact email',lead['email'] or 'Not provided')
  label('Group',f"{len(members)} participants")
 y-=12;cv.setStrokeColor(HexColor('#dee4db'));cv.line(42,y,W-42,y);y-=21
 cv.setFillColor(HexColor('#263a31'));cv.setFont('Helvetica-Bold',10)
 cv.drawString(44,y,'DESCRIPTION');cv.drawRightString(410,y,'QTY');cv.drawRightString(479,y,'UNIT');cv.drawRightString(548,y,'TOTAL');y-=25
 def line(desc,qty,amount):
  nonlocal y
  if y<190:
   cv.showPage();y=H-70
  cv.setFont('Helvetica',9);cv.setFillColor(HexColor('#23392e'))
  cv.drawString(44,y,desc[:53]);cv.drawRightString(410,y,str(qty));cv.drawRightString(479,y,f'{amount/max(1,qty)/100:.2f}');cv.drawRightString(548,y,f'EUR {amount/100:.2f}');y-=19
 if mid:
  line(f'Payment share — {b["split_mode"].capitalize()} split',1,charge['amount_cents'])
  cv.setFont('Helvetica-Oblique',8);cv.drawString(44,y,'The consolidated invoice contains all group line items.');y-=18
  gross=charge['amount_cents']
 else:
  for member in members:
   line(f"Studio seat — {member['name']}"+(' (10-stamp reward)' if member['loyal'] else ''),1,LOYALTY if member['loyal'] else BASE)
  if bill['room_fee_cents']:line('Private studio reservation',1,ROOM_FEE)
  else:line('Private studio reservation (loyalty benefit)',1,0)
  for item in items:
   line(MENUD[item['product_id']]['name']+('  (shared)' if item['shared'] else ''),item['qty'],item['qty']*item['unit_cents'])
  gross=bill['total_cents']
 if bill['loyalty_savings_cents']:
  cv.setFont('Helvetica-Oblique',9);cv.setFillColor(HexColor('#27805a'))
  cv.drawString(44,y,f'10-stamp loyalty savings already applied: EUR {bill["loyalty_savings_cents"]/100:.2f}');y-=20
 y-=5;cv.line(42,y,W-42,y);y-=24
 tax=round(gross*VAT_PCT/(100+VAT_PCT))
 def val(lbl,number,bold=False):
  nonlocal y
  cv.setFillColor(HexColor('#1d362a'));cv.setFont('Helvetica-Bold' if bold else 'Helvetica',11 if bold else 9)
  cv.drawString(330,y,lbl);cv.drawRightString(548,y,f'EUR {number/100:.2f}');y-=22
 val('Subtotal (excl. est. VAT)',gross-tax)
 val(f'VAT included ({VAT_PCT}% illustrative)',tax)
 val('TOTAL PAID',gross,True)
 y-=14;cv.setFont('Helvetica-Bold',9);cv.drawString(44,y,'PAYMENT STATUS: PAID');y-=20
 cv.setFont('Helvetica',8);cv.drawString(44,y,'Amount in EUR  •  Payment records are verified by the server.');y-=20
 if DEMO:
  cv.setFont('Helvetica-Bold',9);cv.setFillColor(HexColor('#b7482e'))
  y=max(y,176)
  cv.drawString(44,y,'DEMO PAYMENT — NOT A FISCAL TAX INVOICE');y-=19
 else:
  cv.setFillColor(HexColor('#69766c'));cv.setFont('Helvetica',8);cv.drawString(44,y,'Business registration: '+BUSINESS_NUMBER[:70]);y-=19
 # QR positioned at bottom to avoid item table collisions.
 qr_y=50; cv.drawImage(ImageReader(io.BytesIO(qr_data)),W-160,qr_y,width=105,height=105)
 cv.setFont('Helvetica-Bold',10);cv.setFillColor(HexColor('#1d362a'));cv.drawString(44,139,'SHOW THIS QR AT CHECK-IN')
 cv.setFont('Helvetica',8);cv.drawString(44,120,'Your private booking QR identifies the room and session.')
 cv.drawString(44,104,'Staff must verify this QR using an authorized check-in.')
 cv.setStrokeColor(HexColor('#e0e7df'));cv.line(42,81,386,81)
 cv.setFont('Helvetica',8);cv.setFillColor(HexColor('#6a766e'))
 cv.drawString(43,64,'Thanks for making memories with Pizza Studio Club!')
 cv.save();return buff.getvalue()
def materialize_documents(bid:str):
 """Generate PDFs automatically for confirmed bookings; files remain private."""
 target=ROOT.parent/'private_invoices'/bid
 target.mkdir(parents=True,exist_ok=True)
 (target/'Group_Invoice.pdf').write_bytes(pdf_invoice(bid))
 with conn() as c:
  mids=[r['id'] for r in c.execute('SELECT id FROM members WHERE booking_id=?',(bid,))]
 for mid in mids:
  (target/f'Receipt_{mid}.pdf').write_bytes(pdf_invoice(bid,mid))

@app.get('/api/bookings/{bid}/invoice.pdf')
def invoice(bid:str,authorization:Optional[str]=Header(None)):
 member=auth(bid,authorization)
 if not member['organizer']:fail(403,'Only the organizer can download the consolidated invoice.')
 data=pdf_invoice(bid)
 return Response(data,media_type='application/pdf',headers={'Content-Disposition':f'attachment; filename="PizzaStudio_Invoice_{bid}.pdf"'})
@app.get('/api/bookings/{bid}/receipt.pdf')
def receipt(bid:str,authorization:Optional[str]=Header(None)):
 member=auth(bid,authorization);data=pdf_invoice(bid,member['id'])
 return Response(data,media_type='application/pdf',headers={'Content-Disposition':f'attachment; filename="PizzaStudio_Receipt_{bid}.pdf"'})

if __name__=='__main__':
 import uvicorn
 uvicorn.run('app.server:app',host='127.0.0.1',port=8000,reload=False)

@app.get('/api/bookings/{bid}/invitation-qr')
def invitation_qr(bid:str,invite:str,authorization:Optional[str]=Header(None)):
 who=auth(bid,authorization);organizer(who)
 with conn() as c:b=booking(c,bid)
 if not hmac.compare_digest(hsh(invite),b['invite_hash']):fail(403,'Invalid invitation.')
 link=f'{ORIGIN}/?invite={urllib.parse.quote(invite)}&booking={urllib.parse.quote(bid)}'
 return Response(qrcode_bytes(link),media_type='image/png')
