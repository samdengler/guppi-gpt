// Viewer request on the /api/* behavior. The edge gateway addresses a runtime target by
// its name as the first path segment, so a project's agent at /api/<name>/invocations is
// the gateway path /<name>/invocations. The platform agent's /api/invocations (target
// "api") and /api/feedback (its own behavior, listed first) are left alone, and so are
// the two names a project cannot take.
var AGENT_PATH = /^\/api\/([a-z0-9-]+)\/invocations$/;
var RESERVED = ["invocations", "feedback"];

function handler(event) {
  var request = event.request;
  var match = AGENT_PATH.exec(request.uri);
  if (match && RESERVED.indexOf(match[1]) === -1) {
    request.uri = "/" + match[1] + "/invocations";
  }
  return request;
}
