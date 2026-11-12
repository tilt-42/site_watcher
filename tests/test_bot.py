"""Offline tests. Run them with ./manage.sh test."""

import asyncio
import functools
import json
import os
import random
import tempfile
from datetime import datetime, timedelta

import httpx
from fake_telegram import CHAT, FakeChat

from site_watcher.logic import (
  TIMEZONE, ChatSettings, PageFormatError, Subscribers, Watcher, day_delay, find_offers, is_night, next_delay,
)
from site_watcher.telegram_bot import (
  LIVE_OFF_TEXT, LIVE_ON_TEXT, LIVE_RETIRED_TEXT, NOT_SUBSCRIBED_TEXT, add_handlers, check_and_notify, live_text,
)


def at(hour, minute, second=0):
  return datetime(2026, 11, 20, hour, minute, second, tzinfo=TIMEZONE)


def make_page(fan_remaining, fan_sold_out=False):
  """Build a page shaped like the real one: the data is a JSON string split across several script pushes."""
  days = [
    {'date': '2026-11-27', 'sessions': []},
    {'date': '2026-11-28', 'sessions': [{'products': [
      {'key': 'vip-day', 'currency': 'CHF', 'choices': [
        {'label': 'Créneau 1', 'remaining': 5, 'priceMinor': 64900, 'soldOut': False},
      ]},
      {'key': 'fan-zone', 'currency': 'CHF', 'choices': [
        {'name': 'Tarif normal', 'remaining': fan_remaining, 'priceMinor': 7400, 'soldOut': fan_sold_out},
        {'name': 'Moins de 7 ans', 'remaining': 0, 'priceMinor': 0, 'soldOut': False},
      ]},
    ]}]},
  ]
  data = '8:["$","x",null,{"initial":{"days":' + json.dumps(days, ensure_ascii=False) + ',"note":"a ] in [ text"}}]\n'
  middle = len(data) // 2
  pushes = [f'<script>self.__next_f.push([1,{json.dumps(part)}])</script>' for part in (data[:middle], data[middle:])]
  return '<html><body><h1>Billets</h1>' + ''.join(pushes) + '</body></html>'


def expect_format_error(html, **kwargs):
  try:
    find_offers(html, **kwargs)
  except PageFormatError:
    return
  raise AssertionError('expected PageFormatError')


def check_parsing():
  offers = find_offers(make_page(3))
  assert [(o.name, o.remaining, o.available) for o in offers] == [('Tarif normal', 3, True), ('Moins de 7 ans', 0, False)]
  assert offers[0].describe() == 'Tarif normal: 3 left (74.00 CHF)'
  assert offers[1].describe() == 'Moins de 7 ans: 0 left (free)'

  assert not find_offers(make_page(3, fan_sold_out=True))[0].available, 'sold out must win over a stale count'
  assert find_offers(make_page(0), product_key='vip-day')[0].available, 'other products must not leak in'

  expect_format_error('<html>Maintenance</html>')
  expect_format_error(make_page(0), date='2026-12-25')
  expect_format_error(make_page(0), product_key='nope')


def check_schedule():
  assert not is_night(at(0, 34, 59)) and is_night(at(0, 35))
  assert is_night(at(6, 54, 59)) and not is_night(at(6, 55))
  assert not is_night(at(14, 0)) and not is_night(at(23, 59))

  assert next_delay(at(1, 0)) == 300
  assert next_delay(at(6, 53)) == 120, 'daytime checks must resume at 06:55'
  assert next_delay(at(6, 54, 59) + timedelta(milliseconds=500)) == 1

  rng = random.Random(1)
  waits = [day_delay(rng) for _ in range(20000)]
  assert min(waits) >= 5 and max(waits) <= 34
  assert 19 < sum(waits) / len(waits) < 21
  inside = sum(10 <= w <= 30 for w in waits) / len(waits)
  below = sum(w < 10 for w in waits) / len(waits)
  above = sum(w > 30 for w in waits) / len(waits)
  assert 0.85 < inside < 0.93 and 0.03 < below < 0.08 and 0.03 < above < 0.08, (inside, below, above)
  assert all(5 <= next_delay(at(14, 0), rng) <= 34 for _ in range(100))

  assert '(night' in live_text(at(1, 0), at(1, 5), find_offers(make_page(0)))
  assert '(night' not in live_text(at(14, 0), at(14, 0, 20), find_offers(make_page(0)))


def check_watcher():
  watcher = Watcher()
  closed, open_ = find_offers(make_page(0)), find_offers(make_page(2))
  assert watcher.update(closed) is None
  assert watcher.update(open_) == 'available'
  assert watcher.update(open_) is None, 'tell people once, not every check'
  assert watcher.update(closed) == 'gone'

  events = [watcher.failed() for _ in range(Watcher.FAILURES_BEFORE_WARNING + 5)]
  assert events.count('broken') == 1 and events[Watcher.FAILURES_BEFORE_WARNING - 1] == 'broken'
  assert watcher.update(closed) == 'recovered'
  assert watcher.update(closed) is None


def check_subscribers(folder):
  path = os.path.join(folder, 'subs.json')
  with open(path, 'w') as file:
    json.dump([5, 3], file)  # The format of the first version.
  subscribers = Subscribers(path)
  assert subscribers.items() == [(3, ChatSettings()), (5, ChatSettings())], 'old files must still load'

  subscribers.change(5, status_message_id=9)
  subscribers.change(99, live_updates=False)  # Not subscribed: ignored.
  subscribers.remove(3)
  subscribers.add(5)  # Already subscribed: keeps its settings.
  assert Subscribers(path).items() == [(5, ChatSettings(live_updates=True, status_message_id=9))]


async def check_conversation(folder):
  page = {'html': make_page(0)}

  async def fetch():
    if page['html'] is None:
      raise httpx.ConnectError('offline')
    return page['html']

  path = os.path.join(folder, 'chat.json')
  chat = FakeChat(functools.partial(add_handlers, subscribers_path=path, fetch=fetch))
  server = chat.server
  subscribers = chat.app.bot_data['subscribers']
  await chat.start()

  async def check(checked_at=at(14, 0), next_at=at(14, 0, 20)):
    """Run one check and return the texts of new messages that notify, in order."""
    before = server.next_id
    await check_and_notify(chat.app, checked_at, next_at)
    return [text for m, (text, _) in sorted(server.messages.items()) if m > before and m not in server.silent]

  def live_id():
    return subscribers.get(CHAT['id']).status_message_id

  def last_reply():
    return server.sent[-1][1]

  await chat.command('/help')
  assert '/checks' in last_reply()
  await chat.command('/checks')
  assert last_reply() == NOT_SUBSCRIBED_TEXT
  assert await check() == [] and server.silent == set(), 'nobody is subscribed yet'

  # Subscribing turns live updates on: one silent message, then edits of that same message.
  await chat.command('/start')
  assert "You're subscribed" in last_reply()
  assert await check() == []
  first_live = live_id()
  assert first_live in server.silent
  assert 'Last check: 14:00:00 · no tickets yet' in chat.text_of(first_live)
  assert 'Next check: about 14:00:20\n' in chat.text_of(first_live)

  edits = server.edits
  assert await check(at(14, 0, 20), at(14, 0, 41)) == []
  assert live_id() == first_live and server.edits == edits + 1
  assert 'Last check: 14:00:20' in chat.text_of(first_live)

  page['html'] = make_page(4)
  [alert] = await check()
  assert 'Tickets available now' in alert and 'Tarif normal: 4 left' in alert, alert
  assert 'Moins de 7 ans' not in alert, 'only list what can be bought'
  assert 'https://tickets.fide.com/fr' in alert
  assert '🎟 4 tickets left!' in chat.text_of(first_live)
  assert await check() == []

  await chat.command('/status')
  assert 'Tarif normal: 4 left' in last_reply() and 'Messages are on' in last_reply()

  page['html'] = make_page(0)
  [gone] = await check()
  assert 'unavailable again' in gone

  page['html'] = None
  messages = [text for _ in range(Watcher.FAILURES_BEFORE_WARNING + 3) for text in await check()]
  assert len(messages) == 1 and 'checks of the ticket page failed' in messages[0], messages
  assert 'could not read the page (13 in a row)' in chat.text_of(first_live)
  await chat.command('/status')
  assert "couldn't read" in last_reply()
  page['html'] = make_page(0)
  assert await check() == ['✅ I can read the ticket page again, and I am back to watching it.']
  assert live_id() == first_live

  # If the person deletes the live message, a new one replaces it, and the new ID is saved.
  del server.messages[first_live]
  assert await check() == []
  second_live = live_id()
  assert second_live != first_live and second_live in server.silent
  assert Subscribers(path).get(CHAT['id']).status_message_id == second_live

  # /checks turns live updates off, then on again with a fresh message.
  await chat.command('/checks')
  assert last_reply() == LIVE_OFF_TEXT and chat.text_of(second_live) == LIVE_RETIRED_TEXT
  edits, before = server.edits, server.next_id
  assert await check() == [] and server.edits == edits and server.next_id == before, 'no updates while off'
  page['html'] = make_page(1)
  assert len(await check()) == 1, 'alerts still arrive with live updates off'

  await chat.command('/checks')
  assert last_reply() == LIVE_ON_TEXT
  await check()
  third_live = live_id()
  assert third_live not in (None, second_live) and third_live in server.silent

  # A chat that blocked the bot is dropped; the others still get the alert.
  subscribers.add(200)
  server.blocked.add(200)
  page['html'] = make_page(0)
  await check()
  page['html'] = make_page(1)
  await check()
  assert 200 not in subscribers and any('Tickets available' in text for chat_id, text in server.sent if chat_id == CHAT['id'])

  await chat.command('/stop')
  assert 'Messages are off' in last_reply() and chat.text_of(third_live) == LIVE_RETIRED_TEXT
  page['html'] = make_page(0)
  await check()
  page['html'] = make_page(2)
  before = server.next_id
  assert await check() == [] and server.next_id == before, 'no messages after /stop'

  await chat.stop()


with tempfile.TemporaryDirectory() as folder:
  check_parsing()
  check_schedule()
  check_watcher()
  check_subscribers(folder)
  asyncio.run(check_conversation(folder))
print('All tests passed.')
