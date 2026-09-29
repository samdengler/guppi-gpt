// Viewer request on the default behavior. A project's page lives at /p/<name>/ and is
// the same bundle as the root page, so /p/<name>/ and /p/<name>/index.html are served
// from /index.html and the page reads <name> from location.pathname. Every other URI,
// including /projects/<name>/... and /p/<name> without the trailing slash, is left alone.
var PAGE_PATH = /^\/p\/[a-z0-9-]+\/(index\.html)?$/;

function handler(event) {
  var request = event.request;
  if (PAGE_PATH.test(request.uri)) {
    request.uri = "/index.html";
  }
  return request;
}
