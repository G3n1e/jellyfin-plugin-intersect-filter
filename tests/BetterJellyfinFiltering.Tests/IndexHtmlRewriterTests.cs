using Jellyfin.Plugin.BetterFiltering.Client;

namespace Jellyfin.Plugin.BetterFiltering.Tests;

/// <summary>
/// The injection transform, and the two things about the shipped script that the transform
/// depends on.
/// </summary>
/// <remarks>
/// This half of the injection mechanism is pure, so it is tested here rather than only
/// observed through a browser. The other half - the header decisions, which need a live
/// response - is covered by prototype/client-test.py against a running server.
/// </remarks>
public class IndexHtmlRewriterTests
{
    private const string Shell =
        "<!doctype html><html><head><title>Jellyfin</title></head>"
        + "<body><div id=\"reactRoot\"></div></body></html>";

    [Fact]
    public void RewritesImmediatelyBeforeTheClosingBodyTag()
    {
        var ok = IndexHtmlRewriter.TryRewrite(Shell, "var x = 1;", out var rewritten);

        Assert.True(ok);
        Assert.Contains("<script>var x = 1;</script></body>", rewritten, StringComparison.Ordinal);
        // The shell either side of the insertion is preserved exactly.
        Assert.StartsWith("<!doctype html>", rewritten, StringComparison.Ordinal);
        Assert.EndsWith("</html>", rewritten, StringComparison.Ordinal);
    }

    [Fact]
    public void MatchesTheClosingTagCaseInsensitively()
    {
        var ok = IndexHtmlRewriter.TryRewrite("<html><BODY></BODY></html>", "x", out var rewritten);

        Assert.True(ok);
        Assert.Contains("<script>x</script></BODY>", rewritten, StringComparison.Ordinal);
    }

    [Fact]
    public void RefusesAShellWithNoClosingBodyTag()
    {
        const string noBody = "<html><div id=\"reactRoot\"></div></html>";

        var ok = IndexHtmlRewriter.TryRewrite(noBody, "x", out var rewritten);

        // Reported as "not applied" rather than patched somewhere plausible: injecting into a
        // shell whose shape has changed is how the script ends up somewhere it never runs.
        Assert.False(ok);
        Assert.Equal(noBody, rewritten);
    }

    [Fact]
    public void InsertsAtTheLastClosingTagNotAnEarlierMention()
    {
        // Rendered HTML sometimes quotes a tag inside a comment or a script. Taking the first
        // match would bury the payload in that comment, where it silently does nothing.
        const string shell = "<html><body><!-- </body> --><div id=\"reactRoot\"></div></body></html>";

        var ok = IndexHtmlRewriter.TryRewrite(shell, "payload", out var rewritten);

        Assert.True(ok);
        Assert.Contains("<!-- </body> -->", rewritten, StringComparison.Ordinal);
        Assert.True(
            rewritten.IndexOf("<script>payload</script>", StringComparison.Ordinal)
                > rewritten.IndexOf("<!-- </body> -->", StringComparison.Ordinal),
            "the script must land after the comment, not inside it");
    }

    [Fact]
    public void RefusesAScriptThatWouldEndTheInlineTagEarly()
    {
        // A "</script" anywhere in the payload, in any case and followed by anything, ends the
        // element as far as the HTML parser is concerned: the rest of the script becomes text
        // on the page. Failing here beats shipping a half-executed script.
        var ex = Assert.Throws<ArgumentException>(
            () => IndexHtmlRewriter.TryRewrite(Shell, "var s = '</script>';", out _));

        Assert.Contains("</script", ex.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void TheEmbeddedScriptIsActuallyEmbedded()
    {
        // Guards the csproj's EmbeddedResource item: without it the provider throws at the
        // first page load, in a container, with no build-time signal at all.
        var script = new ClientScriptProvider().Script;

        Assert.False(string.IsNullOrWhiteSpace(script));
        Assert.Contains("__JF_INTERSECT__", script, StringComparison.Ordinal);
    }

    [Fact]
    public void TheEmbeddedScriptCannotTerminateItsOwnTag()
    {
        // The same rule the rewriter enforces, asserted against the file that actually ships,
        // so the two cannot drift.
        var script = new ClientScriptProvider().Script;

        Assert.DoesNotContain("</script", script, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public void TheEmbeddedScriptSurvivesInjection()
    {
        var script = new ClientScriptProvider().Script;

        var ok = IndexHtmlRewriter.TryRewrite(Shell, script, out var rewritten);

        Assert.True(ok);
        Assert.Contains(script, rewritten, StringComparison.Ordinal);
    }

    [Fact]
    public void TheEmbeddedScriptTargetsThePluginEndpoint()
    {
        // The one string that has to agree with IntersectController's route. If the route is
        // ever renamed this fails here instead of 404ing in a browser.
        var script = new ClientScriptProvider().Script;

        Assert.Contains("BetterJellyfinFiltering/Intersect", script, StringComparison.Ordinal);
    }
}
