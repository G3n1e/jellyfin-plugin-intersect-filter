namespace Jellyfin.Plugin.BetterFiltering.Client;

/// <summary>
/// Splices the client script into the web client's SPA shell.
/// </summary>
/// <remarks>
/// Deliberately pure: the injection mechanism is a string transform plus a set of header
/// decisions, and only the headers need a live request to exercise. Keeping the transform
/// separate means the interesting half is unit testable (see <c>IndexHtmlRewriterTests</c>)
/// rather than only observable through a browser.
/// </remarks>
public static class IndexHtmlRewriter
{
    /// <summary>
    /// The string every injection mechanism has to match, because
    /// <c>jellyfin-web</c>'s <c>src/index.html</c> is a bare <c>&lt;div id="reactRoot"&gt;</c>
    /// with no plugin hook. This shared fragility is documented in docs/FINDINGS.md.
    /// </summary>
    private const string BodyCloseTag = "</body>";

    /// <summary>
    /// Inserts <paramref name="script"/> as an inline script immediately before the last
    /// closing body tag.
    /// </summary>
    /// <param name="html">The served shell.</param>
    /// <param name="script">The script body, without any surrounding tags.</param>
    /// <param name="rewritten">The patched shell, or <paramref name="html"/> when nothing was done.</param>
    /// <returns><c>true</c> when the shell was patched.</returns>
    /// <remarks>
    /// The last <c>&lt;/body&gt;</c> is used rather than the first, and the search is
    /// case-insensitive: a shell that mentions the tag in a comment earlier in the document
    /// must not take the script with it into a comment.
    /// </remarks>
    public static bool TryRewrite(string html, string script, out string rewritten)
    {
        ArgumentNullException.ThrowIfNull(html);
        ArgumentNullException.ThrowIfNull(script);

        rewritten = html;

        var index = html.LastIndexOf(BodyCloseTag, StringComparison.OrdinalIgnoreCase);
        if (index < 0)
        {
            return false;
        }

        // An inline script ends at the first "</script" in its own text, in any case and
        // whatever follows it, so a payload containing that sequence would be truncated
        // mid-statement and fail in the browser rather than here.
        if (script.Contains("</script", StringComparison.OrdinalIgnoreCase))
        {
            throw new ArgumentException(
                "The client script contains a '</script' sequence, which would terminate the "
                + "inline script tag early. Embed it as an external file instead.",
                nameof(script));
        }

        rewritten = string.Concat(html[..index], "<script>", script, "</script>", html[index..]);
        return true;
    }
}
