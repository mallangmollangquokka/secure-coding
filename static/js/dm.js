document.addEventListener('DOMContentLoaded', function () {
  var dmDiv = document.getElementById('dm');
  if (!dmDiv) return;

  var roomId = dmDiv.dataset.roomId;
  var socket = io();

  socket.on('connect', function () {
    socket.emit('join_dm', { room_id: roomId });
  });

  socket.on('dm_message', function (data) {
    var messages = document.getElementById('dm_messages');
    var item = document.createElement('li');
    item.textContent = data.username + ': ' + data.message;
    messages.appendChild(item);
    window.scrollTo(0, document.body.scrollHeight);
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
