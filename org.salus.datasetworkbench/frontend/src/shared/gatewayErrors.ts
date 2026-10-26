/**
 * What to tell the user about a failed call to the backend. The workbench's own errors always have a message in the body and are
 * shown as they are. An answer with an EMPTY body comes from Salus itself, not from the workbench, and has a typical cause.
 */
export function explainFailure(status: number, body: string, statusText: string, method: string, path: string): string {
  if (body.trim()) return body;
  const request = `${method} ${path.split("?")[0]}`;
  switch (status) {
    case 404:
      return (
        `Salus did not pass the request on (404 for ${request}). The connection between this browser page and the server was probably closed ` +
        "(an idle timeout of a proxy, a network change, a server restart). Reload the Salus page."
      );
    case 401:
    case 403:
      return `Salus refused the request (${status} for ${request}). Your login may have expired: reload the Salus page.`;
    case 502:
    case 503:
    case 504:
      return `The workbench backend did not answer (${status} for ${request}). It may be busy or stopped; try again in a moment.`;
    default:
      return `${status} ${statusText}`.trim() || `Request failed (${status}).`;
  }
}
