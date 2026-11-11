"""The bot's rules. Nothing here knows about Telegram, so the rules stay easy to test and reuse."""

import json
import os
import re
from dataclasses import dataclass

# What to watch. The ticket page is a Next.js site that embeds the data for every day in the HTML.
PAGE_URL = 'https://tickets.fide.com/fr'
WATCH_DATE = '2026-11-28'
WATCH_PRODUCT = 'fan-zone'
WATCH_LABEL = 'Pass Fan Zone, Saturday 28 November'

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


class Watcher:
  """Remembers the last result and decides when people should be told about a change."""

  FAILURES_BEFORE_WARNING = 10

  def __init__(self):
    self.available = False
    self.failures = 0
    self.warned = False

  def update(self, offers):
    """Return 'available' when tickets appear, 'gone' when they disappear, otherwise None."""
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


class Subscribers:
  """Chat IDs that get alerts, saved to a file so that they survive restarts and deployments."""

  def __init__(self, path):
    self.path = path
    try:
      with open(path, encoding='utf-8') as file:
        self.chat_ids = set(json.load(file))
    except FileNotFoundError:
      self.chat_ids = set()

  def add(self, chat_id):
    self.chat_ids.add(chat_id)
    self._save()

  def remove(self, chat_id):
    self.chat_ids.discard(chat_id)
    self._save()

  def __contains__(self, chat_id):
    return chat_id in self.chat_ids

  def __iter__(self):
    return iter(sorted(self.chat_ids))

  def _save(self):
    temporary = f'{self.path}.tmp'
    with open(temporary, 'w', encoding='utf-8') as file:
      json.dump(sorted(self.chat_ids), file)
    os.replace(temporary, self.path)
