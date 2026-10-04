using System.Reflection;
using Jellyfin.Plugin.BetterFiltering;
using MediaBrowser.Common.Configuration;
using MediaBrowser.Model.Serialization;
using Moq;

namespace Jellyfin.Plugin.BetterFiltering.Tests;

/// <summary>
/// The settings page, checked against the plugin that declares it.
/// </summary>
/// <remarks>
/// None of this is reachable from a unit test at run time: the host serves the page out of
/// the assembly by resource name, into a dashboard iframe-less view, in a browser. What can
/// be checked cheaply is that the three things which must agree do agree, because each of
/// them fails silently and only in the dashboard:
/// <list type="bullet">
/// <item>the resource name <c>GetPages()</c> declares and the name the csproj actually embeds;</item>
/// <item>the plugin id hardcoded in the page and the id the host knows the plugin by;</item>
/// <item>the settings that exist and the settings the page can edit.</item>
/// </list>
/// </remarks>
public class ConfigPageTests
{
    /// <summary>
    /// Builds a plugin over mocked host paths. The base constructor combines
    /// <c>PluginsPath</c> with the assembly location and touches nothing on disk.
    /// </summary>
    private static Plugin CreatePlugin()
    {
        var paths = new Mock<IApplicationPaths>();
        paths.SetupGet(p => p.PluginsPath).Returns(Path.GetTempPath());
        paths.SetupGet(p => p.PluginConfigurationsPath).Returns(Path.GetTempPath());

        return new Plugin(paths.Object, new Mock<IXmlSerializer>().Object);
    }

    private static string ReadPage(Plugin plugin)
    {
        var page = Assert.Single(plugin.GetPages());
        var assembly = typeof(Plugin).Assembly;

        using var stream = assembly.GetManifestResourceStream(page.EmbeddedResourcePath);
        Assert.NotNull(stream);

        using var reader = new StreamReader(stream);
        return reader.ReadToEnd();
    }

    [Fact]
    public void TheDeclaredResourceNameIsTheOneTheBuildEmbeds()
    {
        // A typo here is invisible until someone opens the settings page and gets a blank
        // frame. GetManifestResourceStream returning null is the whole failure.
        var plugin = CreatePlugin();
        var page = Assert.Single(plugin.GetPages());

        Assert.Contains(page.EmbeddedResourcePath, typeof(Plugin).Assembly.GetManifestResourceNames());
        Assert.False(string.IsNullOrWhiteSpace(ReadPage(plugin)));
    }

    [Fact]
    public void ThePageConfiguresThisPluginAndNotAnother()
    {
        // The page talks to /Plugins/{id}/Configuration. A stale id there saves the user's
        // settings to a plugin that does not exist, or worse, to a different one.
        var plugin = CreatePlugin();

        Assert.Contains(plugin.Id.ToString(), ReadPage(plugin), StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public void EveryConfigurationSettingHasAControl()
    {
        // Adding a setting to PluginConfiguration and forgetting the page leaves it editable
        // only by hand-editing XML - which is the situation this page exists to end.
        var html = ReadPage(CreatePlugin());

        var settings = typeof(PluginConfiguration)
            .GetProperties(BindingFlags.Public | BindingFlags.Instance)
            .Where(p => p.DeclaringType == typeof(PluginConfiguration))
            .Select(p => p.Name)
            .ToArray();

        Assert.NotEmpty(settings);
        foreach (var setting in settings)
        {
            Assert.Contains(setting, html, StringComparison.Ordinal);
        }
    }

    [Fact]
    public void ThePageOffersTheInjectionToggle()
    {
        // Specifically, not just "some control": this is the documented recovery step when a
        // web-client upgrade breaks the injection, and it is the reason the page was built.
        var html = ReadPage(CreatePlugin());

        Assert.Contains("InjectClientScript", html, StringComparison.Ordinal);
        Assert.Contains("checkbox", html, StringComparison.Ordinal);
    }
}
