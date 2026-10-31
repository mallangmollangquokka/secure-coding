document.addEventListener('DOMContentLoaded', function () {
  var dmDiv = document.getElementById('dm');
  if (!dmDiv) return;

  var roomId = dmDiv.dataset.roomId;
  var other = dmDiv.dataset.other || '';
  var socket = io();

  socket.on('connect', function () {
    socket.emit('join_dm', { room_id: roomId });
  });

  function appendBubble(username, message) {
    var messages = document.getElementById('dm_messages');
    var mine = (username !== other);  // 1:1 대화: 상대가 아니면 내 메시지
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
      row.appendChild(avatar);
      row.appendChild(bubble);
    }
    messages.appendChild(row);
    messages.scrollTop = messages.scrollHeight;
  }

  socket.on('dm_message', function (data) {
    appendBubble(data.username, data.message);
  });

  socket.on('rate_limited', function () {
    window.alert('메시지를 너무 빠르게 보내고 있습니다. 잠시 후 다시 시도해주세요.');
  });

  function sendMessage() {
    var input = document.getElementById('dm_input');
    var message = input.value;
    if (message) {
      socket.emit('send_dm', { room_id: roomId, message: message });
      input.value = '';
    }
  }

  document.getElementById('dm_send_btn').addEventListener('click', sendMessage);
  document.getElementById('dm_input').addEventListener('keydown', function (e) {
    if (e.key === 'Enter') {
      e.preventDefault();
      sendMessage();
    }
  });
});
