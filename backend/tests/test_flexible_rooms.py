"""Run with PYTHONPATH=. python3 tests/test_flexible_rooms.py."""
import datetime as dt
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

fd, database = tempfile.mkstemp(suffix='.sqlite3')
os.close(fd)
os.environ['PIZZA_DB'] = database
os.environ['DEMO_MODE'] = 'true'
os.environ['SECRET_KEY'] = 'test-key-for-flexible-room-qr-signatures'
os.environ['STAFF_PIN'] = 'test-staff-pin'

from fastapi import HTTPException
from app.server import (AddItem, Join, NewBooking, Payment, Split, available, calc_bill,
                        checkout, conn, create, demo_pay, get_book, join, pdf_invoice,
                        qr_token, set_guest_count, slots, verify_qr, issue_demo_reward)


def day(offset):
 return (dt.date.today() + dt.timedelta(days=offset)).isoformat()


def book(count, offset, time='18:00', room=None, loyal=False, extras=0):
 reward_code=issue_demo_reward('test-staff-pin')['reward_code'] if loyal else ''
 result=create(NewBooking(date=day(offset),time=time,name='Organizer',expected_guests=count,
                          requested_room=room,loyal=loyal,reward_code=reward_code,extra_food_packages=extras))
 people=[result]
 for n in range(count-1):
  people.append(join(result['booking_id'],Join(invite=result['invite_token'],name=f'Guest {n+1}')))
 return result,people


def header(person):return 'Bearer '+person['member_token']


def lock(result,mode='individual',custom=None,payer=None):
 return checkout(result['booking_id'],Split(mode=mode,custom=custom or {},payer_id=payer),header(result))


def rejected(fn,status):
 try:fn()
 except HTTPException as error:assert error.status_code==status,error.detail
 else:raise AssertionError(f'Expected HTTP {status}')


def test_sizes_and_pricing():
 for count,offset in [(2,2),(8,3),(10,4),(12,5),(16,6)]:
  result,people=book(count,offset,room='1+2' if count>8 else 1)
  view=get_book(result['booking_id'],header(result))
  bill=view['bill']
  assert bill['duo_package_count']==(count+1)//2
  assert bill['total_cents']==2200*((count+1)//2)
  assert bill['room_fee_cents']==0
  locked=lock(result)
  assert locked['booking']['room_name']==('Combined Studio 1 + 2' if count>8 else 'Studio 1')
  assert len(locked['charges'])==count
  assert sum(x['amount_cents'] for x in locked['charges'])==bill['total_cents']
  if count==10:
   for person in people:demo_pay(result['booking_id'],Payment(idempotency_key='test-key'),header(person))
   with conn() as c:token=qr_token(c.execute('SELECT * FROM bookings WHERE id=?',(result['booking_id'],)).fetchone())
   assert verify_qr(token)['room_name']=='Combined Studio 1 + 2'
   assert pdf_invoice(result['booking_id']).startswith(b'%PDF')
  print('PASS group size',count)


def test_included_food_loyalty_and_extra():
 result,people=book(2,7,loyal=True,extras=1)
 from app.server import add_item
 for product in ('pizza_margherita','pizza_pepperoni','cola','water'):
  add_item(result['booking_id'],AddItem(product_id=product),header(result))
 view=get_book(result['booking_id'],header(result))
 assert view['bill']['duo_total_cents']==1400
 assert view['bill']['extra_food_cents']==1200
 assert view['bill']['food_cents']==0
 assert sum(x['included_qty'] for x in view['items'])==4
 add_item(result['booking_id'],AddItem(product_id='pizza_bbq'),header(people[1]))
 view=get_book(result['booking_id'],header(result))
 assert view['bill']['food_cents']==1190  # Most expensive pizzas are included first.
 assert view['bill']['total_cents']==1400+1200+1190
 assert sum(x['amount_cents'] for x in lock(result)['charges'])==view['bill']['total_cents']
 print('PASS included food and loyalty pricing')


def test_one_time_reward_and_odd_group():
 code=issue_demo_reward('test-staff-pin')['reward_code']
 first=create(NewBooking(date=day(18),time='18:00',name='Reward guest',expected_guests=3,
                         loyal=True,reward_code=code))
 join(first['booking_id'],Join(invite=first['invite_token'],name='Second'))
 join(first['booking_id'],Join(invite=first['invite_token'],name='Third'))
 bill=get_book(first['booking_id'],header(first))['bill']
 assert bill['duo_package_count']==2 and bill['duo_total_cents']==1400+2200
 rejected(lambda:create(NewBooking(date=day(19),time='18:00',name='Reuse',expected_guests=2,
                                    loyal=True,reward_code=code)),409)
 print('PASS one-time reward and odd group rounding')


def test_room_conflicts_and_suggestions():
 pair,people=book(10,8,room='1+2');lock(pair)
 singles=slots(day(8),guests=2)
 at18=next(x for x in singles['slots'] if x['start']=='18:00')
 assert 1 not in at18['rooms'] and 2 not in at18['rooms']
 rejected(lambda:lock(book(2,8,room=2)[0]),409)
 other_pair=slots(day(8),guests=12)
 assert next(x for x in other_pair['slots'] if x['start']=='18:00')['rooms']==['3+4']
 individual,_=book(2,9,room=3);lock(individual)
 assert '3+4' not in next(x for x in slots(day(9),guests=10)['slots'] if x['start']=='18:00')['rooms']
 rejected(lambda:lock(book(10,9,room='3+4')[0]),409)
 assert slots(day(9),guests=10)['slots']
 rejected(lambda:create(NewBooking(date=day(10),time='20:00',name='Late',expected_guests=10)),422)
 print('PASS room conflicts and operating hours')


def test_all_splits_and_atomic_checkout():
 for index,mode in enumerate(('individual','equal','custom','one')):
  result,people=book(10,11+index,room='3+4')
  total=get_book(result['booking_id'],header(result))['bill']['total_cents']
  custom={p['member_id']:total//10 for p in people}
  view=lock(result,mode,custom if mode=='custom' else None,people[0]['member_id'])
  assert sum(x['amount_cents'] for x in view['charges'])==total
  if mode=='equal':assert max(x['amount_cents'] for x in view['charges'])-min(x['amount_cents'] for x in view['charges'])<=1
  if mode=='one':assert sum(x['amount_cents']>0 for x in view['charges'])==1
  print('PASS split',mode)
 a,_=book(10,16,room='1+2');b,_=book(10,16,room='1+2')
 with ThreadPoolExecutor(max_workers=2) as pool:
  results=list(pool.map(lambda booking:try_lock(booking),[a,b]))
 assert sorted(results)==[200,409],results
 print('PASS simultaneous checkout serialization')


def try_lock(result):
 try:lock(result);return 200
 except HTTPException as error:return error.status_code


if __name__=='__main__':
 test_sizes_and_pricing()
 test_included_food_loyalty_and_extra()
 test_one_time_reward_and_odd_group()
 test_room_conflicts_and_suggestions()
 test_all_splits_and_atomic_checkout()
 print('All flexible room tests passed.')
