using MediaBrowser.Common.Configuration;
using MediaBrowser.Common.Plugins;
using MediaBrowser.Model.Plugins;
using MediaBrowser.Model.Serialization;

namespace Jellyfin.Plugin.BetterFiltering;

/// <summary>
/// Plugin entry point.
/// </summary>
public class Plugin : BasePlugin<PluginConfiguration>, IHasPluginConfiguration
{
    /// <summary>
    /// Fixed plugin id. This MUST be a hardcoded constant: the host derives the
    /// configuration directory and the plugin's identity from it, so an id that
    /// changed between builds would silently orphan the saved configuration.
    /// </summary>
    private static readonly Guid PluginId = Guid.Parse("8f3c1a52-6d24-4b7e-9c8a-1f2e3d4c5b6a");

    /// <summary>
    /// Initializes a new instance of the <see cref="Plugin"/> class.
    /// </summary>
    /// <param name="applicationPaths">Instance of the <see cref="IApplicationPaths"/> interface.</param>
    /// <param name="xmlSerializer">Instance of the <see cref="IXmlSerializer"/> interface.</param>
    public Plugin(IApplicationPaths applicationPaths, IXmlSerializer xmlSerializer)
        : base(applicationPaths, xmlSerializer)
    {
        Instance = this;
    }

    /// <summary>
    /// Gets the current plugin instance.
    /// </summary>
    public static Plugin? Instance { get; private set; }

    /// <inheritdoc />
    public override string Name => "Better Jellyfin Filtering";

    /// <inheritdoc />
    public override string Description =>
        "Filters a library by intersecting multiple values from the same category " +
        "(genre AND genre AND ...) instead of unioning them, removing the stock " +
        "two-group ceiling on /Items.";

    /// <inheritdoc />
    public override Guid Id => PluginId;
}
