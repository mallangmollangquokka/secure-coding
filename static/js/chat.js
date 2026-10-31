document.addEventListener('DOMContentLoaded', function () {
  var chatDiv = document.getElementById('chat');
  if (!chatDiv) return;

  var socket = io();
  var me = chatDiv.dataset.me || '';

  socket.on('connect', function () {
    console.log('채팅 서버에 연결됨');
  });

  function appendBubble(username, message, mine) {
    var messages = document.getElementById('messages');
    var row = document.createElement('div');
    row.className = 'bubble-row ' + (mine ? 'me' : 'other');

    var bubble = document.createElement('div');
    bubble.className = 'bubble';
    bubble.textContent = message;  // textContent로 삽입 → XSS 방지

    if (mine) {
      row.appendChild(bubble);
    } else {
      var avatar = document.createElement('div');
      avatar.className = 'avatar';
      avatar.textContent = (username || '?').charAt(0);
      var col = document.createElement('div');
      col.className = 'bubble-col';
      var name = document.createElement('div');
      name.className = 'bubble-name';
      name.textContent = username;
      col.appendChild(name);
      col.appendChild(bubble);
      row.appendChild(avatar);
      row.appendChild(col);
    }
    messages.appendChild(row);
    messages.scrollTop = messages.scrollHeight;
  }

  socket.on('message', function (data) {
    appendBubble(data.username, data.message, data.username === me);
  });

  socket.on('rate_limited', function () {
    window.alert('메시지를 너무 빠르게 보내고 있습니다. 잠시 후 다시 시도해주세요.');
  });

  function sendMessage() {
    var input = document.getElementById('chat_input');
    var message = input.value;
    if (message) {
      socket.emit('send_message', { message: message });
      input.value = '';
    }
  }

  document.getElementById('chat_send_btn').addEventListener('click', sendMessage);
  document.getElementById('chat_input').addEventListener('keydown', function (e) {
    if (e.key === 'Enter') {
      e.preventDefault();
      sendMessage();
    }
  });
});
