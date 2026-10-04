using MediaBrowser.Model.Plugins;

namespace Jellyfin.Plugin.BetterFiltering;

/// <summary>
/// Plugin configuration.
/// </summary>
public class PluginConfiguration : BasePluginConfiguration
{
    /// <summary>
    /// Gets or sets the maximum number of ids a single criterion may contribute
    /// before the request is rejected.
    /// </summary>
    /// <remarks>
    /// This is a guard rail, not a page size. Exceeding it produces an explicit
    /// error rather than a truncated result, because a truncated set that is
    /// then intersected yields a number that looks like a normal answer and is
    /// simply wrong (docs/FINDINGS.md section 5).
    /// </remarks>
    public int MaxCriterionIds { get; set; } = 250_000;

    /// <summary>
    /// Gets or sets the default page size when a request does not specify one.
    /// </summary>
    public int DefaultPageSize { get; set; } = 100;

    /// <summary>
    /// Gets or sets the largest page a caller may request.
    /// </summary>
    public int MaxPageSize { get; set; } = 500;

    /// <summary>
    /// Gets or sets a value indicating whether the client UI is injected into the web client.
    /// </summary>
    /// <remarks>
    /// This exists as an escape hatch, not a feature: the injection works by matching
    /// <c>&lt;/body&gt;</c> in a shell the web client does not intend anyone to modify, so a
    /// web-client upgrade can break it. Turning it off restores a stock client without
    /// removing the plugin, leaving the intersection endpoint usable directly.
    /// </remarks>
    /// <seealso cref="Plugin.GetPages" />
    public bool InjectClientScript { get; set; } = true;
}
