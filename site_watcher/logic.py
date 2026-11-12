"""The bot's rules. Nothing here knows about Telegram, so the rules stay easy to test and reuse."""

import json
import os
import random
import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime, time
from typing import Optional
from zoneinfo import ZoneInfo

# What to watch. The ticket page is a Next.js site that embeds the data for every day in the HTML.
PAGE_URL = 'https://tickets.fide.com/fr'
WATCH_DATE = '2026-11-28'
WATCH_PRODUCT = 'fan-zone'
WATCH_LABEL = 'Pass Fan Zone, Saturday 28 November'

# When to check. At night the bot checks every 5 minutes; during the day it waits a random time, about 20 s on average.
TIMEZONE = ZoneInfo('Europe/Zurich')
NIGHT_START = time(0, 35)
NIGHT_END = time(6, 55)
NIGHT_INTERVAL_SECONDS = 300
MIN_INTERVAL_SECONDS = 5

# Each chunk of embedded page data looks like: self.__next_f.push([1,"<JSON string>"])
FLIGHT_CHUNK = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)')


class PageFormatError(ValueError):
  """The page no longer has the data in the expected shape, so the bot can't tell what is on sale."""


@dataclass(frozen=True)
class Offer:
  name: str
  remaining: int
  price_minor: int
  currency: str

  @property
  def available(self):
    return self.remaining > 0

  def describe(self):
    price = 'free' if self.price_minor == 0 else f'{self.price_minor / 100:.2f} {self.currency}'
    return f'{self.name}: {self.remaining} left ({price})'


def embedded_days(html):
  """Return the list of days from the page's embedded data."""
  chunks = FLIGHT_CHUNK.findall(html)
  if not chunks:
    raise PageFormatError('No embedded page data found')
  data = ''.join(json.loads(chunk) for chunk in chunks)

  marker = '"days":['
  start = data.find(marker)
  if start < 0:
    raise PageFormatError('No list of days found')
  try:
    days, _ = json.JSONDecoder().raw_decode(data, start + len(marker) - 1)
  except json.JSONDecodeError as error:
    raise PageFormatError(f'The list of days is not valid JSON: {error}') from None
  return days


def find_offers(html, date=WATCH_DATE, product_key=WATCH_PRODUCT):
  """Return the offers (price categories) of one product on one day."""
  days = embedded_days(html)
  try:
    day = next(d for d in days if d.get('date') == date)
    product = next(p for s in day['sessions'] for p in s['products'] if p.get('key') == product_key)
    offers = [
      Offer(
        name=choice.get('name') or choice.get('label') or 'Ticket',
        remaining=0 if choice.get('soldOut') else int(choice.get('remaining') or 0),
        price_minor=int(choice.get('priceMinor') or 0),
        currency=product.get('currency', 'CHF'),
      )
      for choice in product['choices']
    ]
  except (StopIteration, KeyError, TypeError, ValueError):
    raise PageFormatError(f'Could not find {product_key} on {date}') from None
  if not offers:
    raise PageFormatError(f'{product_key} on {date} has no price categories')
  return offers


def is_night(moment):
  return NIGHT_START <= moment.time() < NIGHT_END


def day_delay(rng=random):
  """A random wait: equally likely anywhere from 10 to 30 s, plus a left-skewed adjustment.

  The adjustment is 4 minus a gamma value with mean 5 (shape 4, scale 1.25), so it averages -1 s: usually
  slightly positive (at most +4 s) and sometimes clearly negative. About 90% of waits fall between 10 and 30 s,
  and the average is 19 s.
  """
  return max(MIN_INTERVAL_SECONDS, rng.uniform(10, 30) + 4 - rng.gammavariate(4, 1.25))


def next_delay(now, rng=random):
  """Seconds to wait before the next check, given the current time in TIMEZONE."""
  if is_night(now):
    night_end = datetime.combine(now.date(), NIGHT_END, tzinfo=now.tzinfo)
    # Don't sleep past the end of the night: resume daytime checks at 06:55.
    return max(1, min(NIGHT_INTERVAL_SECONDS, (night_end - now).total_seconds()))
  return day_delay(rng)


class Watcher:
  """Remembers the last result and decides when people should be told about a change."""

  FAILURES_BEFORE_WARNING = 10

  def __init__(self):
    self.available = False
    self.failures = 0
    self.warned = False

  def update(self, offers):
    """Return 'available' when tickets appear, 'gone' when they disappear, 'recovered' after a warning, else None."""
    recovered = self.warned
    self.failures = 0
    self.warned = False

    now_available = any(offer.available for offer in offers)
    if now_available == self.available:
      return 'recovered' if recovered else None
    self.available = now_available
    return 'available' if now_available else 'gone'

  def failed(self):
    """Return 'broken' once, after several failed checks in a row, otherwise None."""
    self.failures += 1
    if self.failures >= self.FAILURES_BEFORE_WARNING and not self.warned:
      self.warned = True
      return 'broken'
    return None


def summary(offers):
  return '\n'.join(f'• {offer.describe()}' for offer in offers)


def total_remaining(offers):
  return sum(offer.remaining for offer in offers)


@dataclass(frozen=True)
class ChatSettings:
  live_updates: bool = True  # Keep one message in the chat that shows the result of every check.
  status_message_id: Optional[int] = None  # That message, once the bot has sent it.


class Subscribers:
  """Chats that get alerts, with their settings. Saved to a file so that they survive restarts and deployments.

  The file is only written when something changes, not on every check, to spare the Pi's SD card.
  """

  def __init__(self, path):
    self.path = path
    try:
      with open(path, encoding='utf-8') as file:
        data = json.load(file)
    except FileNotFoundError:
      data = {}
    if isinstance(data, list):  # The first version saved a plain list of chat IDs.
      data = {chat_id: {} for chat_id in data}
    self.chats = {int(chat_id): ChatSettings(**settings) for chat_id, settings in data.items()}

  def add(self, chat_id):
    if chat_id not in self.chats:
      self.chats[chat_id] = ChatSettings()
      self._save()

  def remove(self, chat_id):
    if self.chats.pop(chat_id, None) is not None:
      self._save()

  def get(self, chat_id):
    return self.chats.get(chat_id)

  def change(self, chat_id, **changes):
    """Change some settings of a subscribed chat. Does nothing if the chat unsubscribed in the meantime."""
    old = self.chats.get(chat_id)
    if old is None:
      return
    new = replace(old, **changes)
    if new != old:
      self.chats[chat_id] = new
      self._save()

  def items(self):
    return sorted(self.chats.items())

  def __contains__(self, chat_id):
    return chat_id in self.chats

  def __iter__(self):
    return iter(sorted(self.chats))

  def _save(self):
    temporary = f'{self.path}.tmp'
    with open(temporary, 'w', encoding='utf-8') as file:
      json.dump({str(chat_id): asdict(settings) for chat_id, settings in sorted(self.chats.items())}, file)
    os.replace(temporary, self.path)
