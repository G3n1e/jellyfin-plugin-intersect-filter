using System.Text;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Http;
using Microsoft.Net.Http.Headers;

namespace Jellyfin.Plugin.BetterFiltering.Client;

/// <summary>
/// Injects the plugin's client script into the web client's SPA shell as it is served.
/// </summary>
/// <remarks>
/// <para>
/// The script is inlined into the shell rather than referenced with
/// <c>&lt;script src="..."&gt;</c>. A separate URL would have to be fetchable by a plain
/// browser request, which carries none of the client's credentials, so it would either need
/// its own anonymous route or a token spliced in at rewrite time — and the shell is served
/// before sign-in, so no token exists yet to splice. Inlining removes the question, the
/// extra round trip and the asset-deployment problem at the cost of a slightly larger
/// document. The server sets no <c>Content-Security-Policy</c>, so an inline script is
/// permitted (verified against Jellyfin 12.1, docs/FINDINGS.md).
/// </para>
/// <para>
/// This is registered as an <see cref="IStartupFilter"/> that adds its middleware before
/// calling <c>next</c>, so it wraps the server's whole pipeline. It has to: the middleware
/// that serves <c>index.html</c> is registered inside that pipeline, and the rewrite can
/// only happen once a body exists.
/// </para>
/// <para>
/// This works by string-matching <c>&lt;/body&gt;</c>, which is the fragile part, and it is
/// fragile because the web client offers no hook — <c>src/index.html</c> is a bare
/// <c>&lt;div id="reactRoot"&gt;</c>. That is the risk the settings page's off switch exists to
/// cover: see <see cref="PluginConfiguration.InjectClientScript"/>.
/// </para>
/// </remarks>
public sealed class ScriptInjectingStartupFilter : IStartupFilter
{
    private static readonly string[] ShellPaths = ["/web", "/web/", "/web/index.html"];

    private readonly ClientScriptProvider _provider;

    /// <summary>
    /// Initializes a new instance of the <see cref="ScriptInjectingStartupFilter"/> class.
    /// </summary>
    /// <param name="provider">Supplies the embedded client script.</param>
    public ScriptInjectingStartupFilter(ClientScriptProvider provider)
    {
        _provider = provider;
    }

    /// <inheritdoc />
    public Action<IApplicationBuilder> Configure(Action<IApplicationBuilder> next)
    {
        ArgumentNullException.ThrowIfNull(next);

        return application =>
        {
            application.Use(RewriteShellAsync);
            next(application);
        };
    }

    private static bool IsShellRequest(HttpRequest request)
    {
        foreach (var path in ShellPaths)
        {
            if (request.Path.Equals(path, StringComparison.OrdinalIgnoreCase))
            {
                return true;
            }
        }

        return false;
    }

    private static bool IsHtml(string? contentType)
        => contentType is not null
           && contentType.StartsWith("text/html", StringComparison.OrdinalIgnoreCase);

    /// <summary>
    /// Removes headers that would serve a stale, unpatched shell.
    /// </summary>
    /// <remarks>
    /// The server marks the shell <c>no-cache</c>, so browsers revalidate it on every load.
    /// Leaving the validators in place would let a revalidation answer 304 with the *patched*
    /// entity tag while the browser replays whatever it cached — which, on the first load
    /// after an upgrade, is the previous version's shell. Dropping them forces a full
    /// response. <c>Vary</c> goes too, since the response no longer varies by
    /// <c>Accept-Encoding</c> once the body is fixed text.
    /// </remarks>
    private static void RemoveCacheValidators(HttpResponse response)
    {
        response.Headers.Remove(HeaderNames.ETag);
        response.Headers.Remove(HeaderNames.LastModified);
        response.Headers.Remove(HeaderNames.Vary);
        response.Headers.Remove(HeaderNames.ContentEncoding);
        response.Headers.Remove(HeaderNames.ContentLength);
    }

    private static bool InjectionEnabled()
        => Plugin.Instance?.Configuration.InjectClientScript ?? true;

    private async Task RewriteShellAsync(HttpContext context, RequestDelegate next)
    {
        // Only a GET carries a shell to rewrite. A HEAD response has no body, so there is no
        // closing body tag to insert before and the static-file handler's own length and
        // validators pass through - they describe the unpatched file, which is what a HEAD of
        // it is. Nothing reads them: navigation is a GET, and a GET never sees them.
        var isGet = HttpMethods.IsGet(context.Request.Method);

        if (!isGet || !IsShellRequest(context.Request) || !InjectionEnabled())
        {
            await next(context).ConfigureAwait(false);
            return;
        }

        // Ask the inner pipeline for a complete, unencoded body. Compression would hand back
        // a gzip stream that cannot be edited as text, and a range request would hand back a
        // fragment that does not contain the closing body tag at all. Stripping the request
        // headers is what makes the response rewriteable; stripping the response
        // Content-Encoding instead would be too late to help.
        var request = context.Request;
        request.Headers.Remove(HeaderNames.AcceptEncoding);
        request.Headers.Remove(HeaderNames.Range);
        request.Headers.Remove(HeaderNames.IfRange);

        var originalBody = context.Response.Body;
        byte[] body;

        using (var buffer = new MemoryStream())
        {
            context.Response.Body = buffer;
            try
            {
                await next(context).ConfigureAwait(false);
            }
            finally
            {
                context.Response.Body = originalBody;
            }

            body = buffer.ToArray();
        }

        var payload = body;

        // HasStarted means the inner pipeline already flushed the response to the transport,
        // so the headers are on the wire and cannot be corrected. Buffering from the start
        // prevents that, but if it happens anyway the only honest option is to pass the
        // unpatched shell through rather than emit a body that disagrees with its headers.
        if (!context.Response.HasStarted
            && context.Response.StatusCode == StatusCodes.Status200OK
            && IsHtml(context.Response.ContentType))
        {
            var html = Encoding.UTF8.GetString(body);

            if (IndexHtmlRewriter.TryRewrite(html, _provider.Script, out var rewritten))
            {
                payload = Encoding.UTF8.GetBytes(rewritten);
                RemoveCacheValidators(context.Response);
                context.Response.ContentLength = payload.Length;
            }
        }

        await originalBody.WriteAsync(payload, context.RequestAborted).ConfigureAwait(false);
    }
}
