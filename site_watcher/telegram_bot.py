"""The Telegram front end. It checks the ticket page on a timer and messages every subscribed chat."""

import logging
import os
from datetime import datetime, timedelta

import httpx
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import Application, CommandHandler

from site_watcher.logic import (
  PAGE_URL, TIMEZONE, WATCH_LABEL, PageFormatError, Subscribers, Watcher, find_offers, is_night, next_delay, summary,
  total_remaining,
)

FIRST_CHECK_SECONDS = 5
USER_AGENT = 'Mozilla/5.0 (compatible; site_watcher/1.0)'

logger = logging.getLogger(__name__)

SCHEDULE_TEXT = 'I check the page about every 20 seconds, and every 5 minutes between 00:35 and 06:55.'
HELP_TEXT = (
  f'I watch the FIDE ticket page for: {WATCH_LABEL}. {SCHEDULE_TEXT}\n\n'
  '/start sends you a message here as soon as tickets become available.\n'
  '/checks turns the live check updates on or off.\n'
  '/status shows the numbers right now.\n'
  '/stop turns all messages off.'
)
SUBSCRIBED_TEXT = (
  f"You're subscribed. {SCHEDULE_TEXT} I'll message you here as soon as tickets for {WATCH_LABEL} "
  'become available.\n\n'
  'I also keep one message here updated with the result of every check. /checks turns it on or off.\n'
  '/status shows the numbers right now. /stop turns all messages off.'
)
UNSUBSCRIBED_TEXT = 'Messages are off for this chat. Send /start to turn them back on.'
NOT_SUBSCRIBED_TEXT = 'This chat is not subscribed. Send /start first.'
LIVE_ON_TEXT = 'Live check updates are on. The next check will appear below, and I will keep updating that one message.'
LIVE_OFF_TEXT = 'Live check updates are off. You still get a message when tickets become available. /checks turns them back on.'
LIVE_RETIRED_TEXT = 'Live check updates are off for this chat.'
BROKEN_TEXT = (
  f"⚠️ My last {Watcher.FAILURES_BEFORE_WARNING} checks of the ticket page failed, so I can't tell whether tickets "
  f'are available. The site may be down, or the page may have changed.\n\nCheck it yourself: {PAGE_URL}'
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


def live_text(checked_at, next_at, offers=None, failures=0):
  """The text of the message that shows the latest check."""
  if offers is None:
    result = f'❌ could not read the page ({failures} in a row)'
  else:
    left = total_remaining(offers)
    result = f'🎟 {left} tickets left!' if left else 'no tickets yet'
  night = ' (night: every 5 min)' if is_night(next_at) else ''
  return (
    f'🔄 {WATCH_LABEL}\n'
    f'Last check: {checked_at:%H:%M:%S} · {result}\n'
    f'Next check: about {next_at:%H:%M:%S}{night}\n\n'
    '/checks turns these updates off.'
  )


async def broadcast(application, text):
  subscribers = application.bot_data['subscribers']
  for chat_id in list(subscribers):
    try:
      await application.bot.send_message(chat_id, text)
    except Forbidden:
      subscribers.remove(chat_id)  # The person blocked the bot or removed it from the group.
    except TelegramError as error:
      logger.warning('Could not message chat %s: %s', chat_id, error)


async def show_live_message(bot, subscribers, chat_id, message_id, text):
  """Edit the chat's live message, or send a new one if it doesn't exist yet or was deleted."""
  if message_id:
    try:
      await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id)
      return
    except BadRequest as error:
      reason = str(error).lower()
      if 'not modified' in reason:
        return
      if 'not found' not in reason and "can't be edited" not in reason:
        raise
  # Sent silently: the phone only buzzes for real news, which goes out as separate messages.
  message = await bot.send_message(chat_id, text, disable_notification=True)
  subscribers.change(chat_id, status_message_id=message.message_id)


async def update_live_messages(application, text):
  subscribers = application.bot_data['subscribers']
  for chat_id, settings in subscribers.items():
    if not settings.live_updates:
      continue
    try:
      await show_live_message(application.bot, subscribers, chat_id, settings.status_message_id, text)
    except Forbidden:
      subscribers.remove(chat_id)
    except TelegramError as error:
      logger.warning('Could not update the live message in chat %s: %s', chat_id, error)


async def retire_live_message(bot, chat_id, message_id, text):
  """Replace the live message's text so that it doesn't look current anymore."""
  if not message_id:
    return
  try:
    await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id)
  except BadRequest:
    pass  # Already deleted, or unchanged.


async def check_and_notify(application, checked_at, next_at):
  """Check the page once, tell subscribers about anything that changed, and update the live messages."""
  data = application.bot_data
  watcher = data['watcher']
  try:
    offers = find_offers(await data['fetch']())
  except (httpx.HTTPError, PageFormatError) as error:
    logger.warning('Check failed: %s', error)
    if watcher.failed() == 'broken':
      await broadcast(application, BROKEN_TEXT)
    await update_live_messages(application, live_text(checked_at, next_at, failures=watcher.failures))
    return

  event = watcher.update(offers)
  if event == 'available':
    await broadcast(application, available_text(offers))
  elif event == 'gone':
    await broadcast(application, gone_text())
  elif event == 'recovered':
    await broadcast(application, RECOVERED_TEXT)
  await update_live_messages(application, live_text(checked_at, next_at, offers))


async def check_job(context):
  """Run one check, then schedule the next one after a random delay (a fixed one at night)."""
  checked_at = datetime.now(TIMEZONE)
  next_at = checked_at + timedelta(seconds=next_delay(checked_at))
  try:
    await check_and_notify(context.application, checked_at, next_at)
  finally:
    # Measured from the start of this check, so a slow page doesn't stretch the interval.
    remaining = (next_at - datetime.now(TIMEZONE)).total_seconds()
    context.job_queue.run_once(check_job, max(1, remaining))


async def subscribe(update, context):
  context.bot_data['subscribers'].add(update.effective_chat.id)
  await update.effective_message.reply_text(SUBSCRIBED_TEXT)


async def unsubscribe(update, context):
  subscribers = context.bot_data['subscribers']
  chat_id = update.effective_chat.id
  settings = subscribers.get(chat_id)
  if settings:
    await retire_live_message(context.bot, chat_id, settings.status_message_id, LIVE_RETIRED_TEXT)
  subscribers.remove(chat_id)
  await update.effective_message.reply_text(UNSUBSCRIBED_TEXT)


async def toggle_live_updates(update, context):
  subscribers = context.bot_data['subscribers']
  chat_id = update.effective_chat.id
  settings = subscribers.get(chat_id)
  if settings is None:
    await update.effective_message.reply_text(NOT_SUBSCRIBED_TEXT)
    return

  if settings.live_updates:
    await retire_live_message(context.bot, chat_id, settings.status_message_id, LIVE_RETIRED_TEXT)
    subscribers.change(chat_id, live_updates=False, status_message_id=None)
    await update.effective_message.reply_text(LIVE_OFF_TEXT)
  else:
    # Start a fresh live message below this reply, where it is easy to find.
    subscribers.change(chat_id, live_updates=True, status_message_id=None)
    await update.effective_message.reply_text(LIVE_ON_TEXT)


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
  application.add_handler(CommandHandler('checks', toggle_live_updates))
  application.add_handler(CommandHandler('status', show_status))
  application.add_handler(CommandHandler('help', show_help))


def main():
  token = os.environ.get('TELEGRAM_BOT_TOKEN')
  if not token:
    raise SystemExit('Please set the TELEGRAM_BOT_TOKEN environment variable first.')

  application = Application.builder().token(token).build()
  add_handlers(application, os.environ.get('SUBSCRIBERS_FILE', 'subscribers.json'))
  application.job_queue.run_once(check_job, FIRST_CHECK_SECONDS)
  application.run_polling()


if __name__ == '__main__':
  main()
