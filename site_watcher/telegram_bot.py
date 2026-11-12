"""The Telegram front end. It checks the ticket page on a timer and messages every subscribed chat."""

import logging
import os

import httpx
from telegram.error import Forbidden, TelegramError
from telegram.ext import Application, CommandHandler

from site_watcher.logic import PAGE_URL, WATCH_LABEL, PageFormatError, Subscribers, Watcher, find_offers, summary

CHECK_INTERVAL_SECONDS = 10
USER_AGENT = 'Mozilla/5.0 (compatible; site_watcher/1.0)'

logger = logging.getLogger(__name__)

HELP_TEXT = (
  f'I watch the FIDE ticket page for: {WATCH_LABEL}.\n\n'
  '/start sends you a message here as soon as tickets become available.\n'
  '/status shows the numbers right now.\n'
  '/stop turns the messages off.'
)
SUBSCRIBED_TEXT = (
  f"You're subscribed. I check the page every 10s and will message you here as soon as tickets for "
  f'{WATCH_LABEL} become available.\n\n/status shows the numbers right now. /stop turns the messages off.'
)
UNSUBSCRIBED_TEXT = 'Messages are off for this chat. Send /start to turn them back on.'
BROKEN_TEXT = (
  "⚠️ I haven't been able to read the ticket page for the last 10 minutes, so I can't tell whether tickets are "
  f'available. The site may be down, or the page may have changed.\n\nCheck it yourself: {PAGE_URL}'
)
RECOVERED_TEXT = '✅ I can read the ticket page again, and I am back to watching it.'


async def fetch_page():
  async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={'User-Agent': USER_AGENT}) as client:
    response = await client.get(PAGE_URL)
    response.raise_for_status()
    return response.text


def available_text(offers):
  available = [offer for offer in offers if offer.available]
  return f'🎟 Tickets available now!\n{WATCH_LABEL}\n\n{summary(available)}\n\nBuy here: {PAGE_URL}'


def gone_text():
  return f'Tickets for {WATCH_LABEL} are unavailable again. I keep watching.'


async def broadcast(application, text):
  subscribers = application.bot_data['subscribers']
  for chat_id in list(subscribers):
    try:
      await application.bot.send_message(chat_id, text)
    except Forbidden:
      subscribers.remove(chat_id)  # The person blocked the bot or removed it from the group.
    except TelegramError as error:
      logger.warning('Could not message chat %s: %s', chat_id, error)


async def check_and_notify(application):
  """Check the page once and tell subscribers about anything that changed."""
  data = application.bot_data
  try:
    offers = find_offers(await data['fetch']())
  except (httpx.HTTPError, PageFormatError) as error:
    logger.warning('Check failed: %s', error)
    if data['watcher'].failed() == 'broken':
      await broadcast(application, BROKEN_TEXT)
    return

  event = data['watcher'].update(offers)
  if event == 'available':
    await broadcast(application, available_text(offers))
  elif event == 'gone':
    await broadcast(application, gone_text())
  elif event == 'recovered':
    await broadcast(application, RECOVERED_TEXT)


async def check_job(context):
  await check_and_notify(context.application)


async def subscribe(update, context):
  context.bot_data['subscribers'].add(update.effective_chat.id)
  await update.effective_message.reply_text(SUBSCRIBED_TEXT)


async def unsubscribe(update, context):
  context.bot_data['subscribers'].remove(update.effective_chat.id)
  await update.effective_message.reply_text(UNSUBSCRIBED_TEXT)


async def show_help(update, context):
  await update.effective_message.reply_text(HELP_TEXT)


async def show_status(update, context):
  try:
    offers = find_offers(await context.bot_data['fetch']())
  except (httpx.HTTPError, PageFormatError) as error:
    logger.warning('Status check failed: %s', error)
    await update.effective_message.reply_text(f"I couldn't read the ticket page just now. Try again, or check {PAGE_URL}")
    return

  subscribed = update.effective_chat.id in context.bot_data['subscribers']
  state = 'Messages are on for this chat.' if subscribed else 'Messages are off. Send /start to turn them on.'
  await update.effective_message.reply_text(f'{WATCH_LABEL}, right now:\n\n{summary(offers)}\n\n{state}')


def add_handlers(application, subscribers_path='subscribers.json', fetch=fetch_page):
  application.bot_data['subscribers'] = Subscribers(subscribers_path)
  application.bot_data['watcher'] = Watcher()
  application.bot_data['fetch'] = fetch

  application.add_handler(CommandHandler('start', subscribe))
  application.add_handler(CommandHandler('stop', unsubscribe))
  application.add_handler(CommandHandler('status', show_status))
  application.add_handler(CommandHandler('help', show_help))


def main():
  token = os.environ.get('TELEGRAM_BOT_TOKEN')
  if not token:
    raise SystemExit('Please set the TELEGRAM_BOT_TOKEN environment variable first.')

  application = Application.builder().token(token).build()
  add_handlers(application, os.environ.get('SUBSCRIBERS_FILE', 'subscribers.json'))
  application.job_queue.run_repeating(check_job, interval=CHECK_INTERVAL_SECONDS, first=5)
  application.run_polling()


if __name__ == '__main__':
  main()
