"""Offline tests. Run them with ./manage.sh test."""

import asyncio
import functools
import json
import os
import tempfile

import httpx
from fake_telegram import CHAT, FakeChat

from site_watcher.logic import PageFormatError, Subscribers, Watcher, find_offers
from site_watcher.telegram_bot import add_handlers, check_and_notify


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


def check_watcher():
  watcher = Watcher()
  closed, open_ = find_offers(make_page(0)), find_offers(make_page(2))
  assert watcher.update(closed) is None
  assert watcher.update(open_) == 'available'
  assert watcher.update(open_) is None, 'tell people once, not every minute'
  assert watcher.update(closed) == 'gone'

  events = [watcher.failed() for _ in range(Watcher.FAILURES_BEFORE_WARNING + 5)]
  assert events.count('broken') == 1 and events[Watcher.FAILURES_BEFORE_WARNING - 1] == 'broken'
  assert watcher.update(closed) == 'recovered'
  assert watcher.update(closed) is None


def check_subscribers(folder):
  path = os.path.join(folder, 'subs.json')
  subscribers = Subscribers(path)
  subscribers.add(5)
  subscribers.add(3)
  subscribers.remove(99)
  assert list(Subscribers(path)) == [3, 5], 'subscribers must survive a restart'


async def check_conversation(folder):
  page = {'html': make_page(0)}

  async def fetch():
    if page['html'] is None:
      raise httpx.ConnectError('offline')
    return page['html']

  chat = FakeChat(functools.partial(add_handlers, subscribers_path=os.path.join(folder, 'chat.json'), fetch=fetch))
  sent = chat.server.sent
  await chat.start()

  async def check():
    before = len(sent)
    await check_and_notify(chat.app)
    return [text for _, text in sent[before:]]

  await chat.command('/help')
  assert '/start' in sent[-1][1]
  assert await check() == [], 'nobody is subscribed yet'

  await chat.command('/start')
  assert "You're subscribed" in sent[-1][1]
  assert await check() == [], 'no tickets yet'

  page['html'] = make_page(4)
  [alert] = await check()
  assert 'Tickets available now' in alert and 'Tarif normal: 4 left' in alert, alert
  assert 'Moins de 7 ans' not in alert, 'only list what can be bought'
  assert 'https://tickets.fide.com/fr' in alert
  assert await check() == []

  await chat.command('/status')
  assert 'Tarif normal: 4 left' in sent[-1][1] and 'Messages are on' in sent[-1][1]

  page['html'] = make_page(0)
  [gone] = await check()
  assert 'unavailable again' in gone

  page['html'] = None
  messages = [text for _ in range(Watcher.FAILURES_BEFORE_WARNING + 3) for text in await check()]
  assert len(messages) == 1 and "haven't been able to read" in messages[0], messages
  await chat.command('/status')
  assert "couldn't read" in sent[-1][1]
  page['html'] = make_page(0)
  assert [text for text in await check()] == ['✅ I can read the ticket page again, and I am back to watching it.']

  # A chat that blocked the bot is dropped; the others still get the alert.
  subscribers = chat.app.bot_data['subscribers']
  subscribers.add(200)
  chat.server.blocked.add(200)
  page['html'] = make_page(1)
  await check()
  assert sent[-1][0] == CHAT['id'] and 200 not in subscribers

  await chat.command('/stop')
  assert 'Messages are off' in sent[-1][1]
  page['html'] = make_page(0)
  await check()
  page['html'] = make_page(2)
  assert await check() == [], 'no messages after /stop'

  await chat.stop()


with tempfile.TemporaryDirectory() as folder:
  check_parsing()
  check_watcher()
  check_subscribers(folder)
  asyncio.run(check_conversation(folder))
print('All tests passed.')
