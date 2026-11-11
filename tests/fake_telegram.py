"""Runs the real python-telegram-bot library against a pretend Telegram server, with no token or network."""

import json

from telegram import Update
from telegram.ext import Application
from telegram.request import BaseRequest

CHAT = {'id': 100, 'type': 'group', 'title': 'Test chat'}
USER = {'id': 7, 'is_bot': False, 'first_name': 'Tester'}
BOT = {'id': 1, 'is_bot': True, 'first_name': 'Bot', 'username': 'test_bot'}


class FakeServer(BaseRequest):
  def __init__(self):
    self.next_id = 1000
    self.messages = {}
    self.alerts = []
    self.sent = []  # (chat_id, text) for every sendMessage, in order.
    self.blocked = set()  # Chat IDs that act like people who blocked the bot.

  @property
  def read_timeout(self):
    return 1.0

  async def initialize(self):
    pass

  async def shutdown(self):
    pass

  def respond(self, result):
    return 200, json.dumps({'ok': True, 'result': result}).encode()

  def message_json(self, message_id, text):
    return {'message_id': message_id, 'date': 0, 'chat': CHAT, 'from': BOT, 'text': text}

  async def do_request(self, url, method, request_data=None, **kwargs):
    params = request_data.parameters if request_data else {}
    markup = params.get('reply_markup')
    if isinstance(markup, str):
      markup = json.loads(markup)
    endpoint = url.rsplit('/', 1)[1]

    if endpoint == 'getMe':
      return self.respond(BOT)
    if endpoint == 'answerCallbackQuery':
      self.alerts.append(params.get('text'))
      return self.respond(True)
    if endpoint == 'sendMessage':
      chat_id = int(params['chat_id'])
      if chat_id in self.blocked:
        return 403, b'{"ok": false, "error_code": 403, "description": "Forbidden: bot was blocked by the user"}'
      self.sent.append((chat_id, params['text']))
      self.next_id += 1
      self.messages[self.next_id] = (params['text'], markup)
      return self.respond(self.message_json(self.next_id, params['text']))
    if endpoint == 'editMessageText':
      message_id = params['message_id']
      if self.messages[message_id] == (params['text'], markup):
        return 400, b'{"ok": false, "description": "Bad Request: message is not modified"}'
      self.messages[message_id] = (params['text'], markup)
      return self.respond(self.message_json(message_id, params['text']))
    if endpoint == 'deleteMessage':
      if params['message_id'] not in self.messages:
        return 400, b'{"ok": false, "description": "Bad Request: message to delete not found"}'
      del self.messages[params['message_id']]
      return self.respond(True)
    raise AssertionError(f'The fake server does not support {endpoint}. Add it to tests/fake_telegram.py.')


class FakeChat:
  """Acts like a person using the bot: sends commands, types replies, and taps buttons."""

  def __init__(self, add_handlers):
    self.server = FakeServer()
    self.update_id = 0
    self.errors = []
    self.app = Application.builder().token('123:FAKE').request(self.server).get_updates_request(FakeServer()).build()
    add_handlers(self.app)

    async def record_error(update, context):
      self.errors.append(context.error)
    self.app.add_error_handler(record_error)

  async def start(self):
    await self.app.initialize()

  async def stop(self):
    await self.app.shutdown()
    assert not self.errors, self.errors

  async def process(self, data):
    self.update_id += 1
    await self.app.process_update(Update.de_json({'update_id': self.update_id, **data}, self.app.bot))

  def new_user_message(self, text):
    self.server.next_id += 1
    return {'message_id': self.server.next_id, 'date': 0, 'chat': CHAT, 'from': USER, 'text': text}

  async def command(self, text):
    message = self.new_user_message(text)
    message['entities'] = [{'type': 'bot_command', 'offset': 0, 'length': len(text.split()[0])}]
    await self.process({'message': message})

  async def reply(self, text, to):
    message = self.new_user_message(text)
    message['reply_to_message'] = {'message_id': to, 'date': 0, 'chat': CHAT, 'text': 'x'}
    await self.process({'message': message})

  async def tap(self, label):
    """Tap the newest button whose label contains the given text."""
    for message_id in sorted(self.server.messages, reverse=True):
      markup = self.server.messages[message_id][1] or {}
      for row in markup.get('inline_keyboard', []):
        for button in row:
          if label in button['text']:
            await self.process({'callback_query': {
              'id': str(self.update_id), 'from': USER, 'chat_instance': 'c', 'data': button['callback_data'],
              'message': {'message_id': message_id, 'date': 0, 'chat': CHAT, 'from': BOT, 'text': 'x'},
            }})
            return message_id
    raise AssertionError(f'No button contains {label}')

  def open_prompts(self):
    return [m for m, (_, markup) in self.server.messages.items() if markup and markup.get('force_reply')]

  def text_of(self, message_id):
    return self.server.messages[message_id][0]
