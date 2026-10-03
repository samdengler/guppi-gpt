// Viewer request on the default behavior. A project's page lives at /p/<name>/ and is
// the same bundle as the root page, so /p/<name>/ and /p/<name>/index.html are served
// from /index.html and the page reads <name> from location.pathname. Every other URI,
// including /projects/<name>/... and /p/<name> without the trailing slash, is left alone.
var PAGE_PATH = /^\/p\/([a-z0-9-]+)\/(index\.html)?$/;
// Projects that changed name, old to new; the old page answers with a permanent redirect
// so bookmarks keep working (hr-connect became hr on 3 Oct 2026, guppi-hr D37).
var RENAMED = { "hr-connect": "hr" };

function handler(event) {
  var request = event.request;
  var match = PAGE_PATH.exec(request.uri);
  if (match && RENAMED[match[1]]) {
    return {
      statusCode: 301,
      statusDescription: "Moved Permanently",
      headers: { location: { value: "/p/" + RENAMED[match[1]] + "/" } },
    };
  }
  if (match) {
    request.uri = "/index.html";
  }
  return request;
}
