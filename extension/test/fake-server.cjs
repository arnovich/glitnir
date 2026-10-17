const http = require('node:http');
const mode = process.argv[2];
if (mode === 'error') { process.stderr.write('fixture startup failed\n'); process.exit(2); }
if (mode === 'timeout') { setInterval(() => {}, 1000); }
else {
  const server = http.createServer((request, response) => {
    if (mode === 'auth' && (!/^[a-f0-9]{64}$/.test(process.env.GLITNIR_SERVER_TOKEN || '') || request.headers['x-glitnir'] !== process.env.GLITNIR_SERVER_TOKEN)) { response.writeHead(403); response.end('{}'); return; }
    if (mode === 'asset' && request.url.startsWith('/asset?')) {
      const name = new URL(request.url, 'http://x').searchParams.get('path');
      if (name === 'assets/a.png') { response.writeHead(200, {'Content-Type': 'image/png'}); response.end('PNG bytes'); }
      else if (name === 'assets/page.png') { response.writeHead(200, {'Content-Type': 'text/html'}); response.end('<script>x</script>'); }
      else { response.writeHead(409, {'Content-Type': 'application/json'}); response.end('{"error":"changed"}'); }
      return;
    }
    if (mode === 'redirect') { response.writeHead(302, {Location: 'https://example.org/'}); response.end(); }
    else { response.setHeader('Content-Type', 'application/json'); response.end(JSON.stringify({claims: [], links: []})); }
  });
  server.listen(0, '127.0.0.1', () => console.log(`Serving Fixture.lean at http://127.0.0.1:${server.address().port}/  (Ctrl-C to stop)`));
  process.on('SIGINT', () => server.close(() => process.exit(0)));
}
