document.addEventListener('DOMContentLoaded', function () {
  var chatDiv = document.getElementById('chat');
  if (!chatDiv) return;

  var username = chatDiv.dataset.username;
  var socket = io();

  socket.on('connect', function () {
    console.log('채팅 서버에 연결됨');
  });

  socket.on('message', function (data) {
    var messages = document.getElementById('messages');
    var item = document.createElement('li');
    item.textContent = data.username + ': ' + data.message;
    messages.appendChild(item);
    window.scrollTo(0, document.body.scrollHeight);
  });

  function sendMessage() {
    var input = document.getElementById('chat_input');
    var message = input.value;
    if (message) {
      socket.emit('send_message', { username: username, message: message });
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
