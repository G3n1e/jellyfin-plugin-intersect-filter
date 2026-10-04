using System.Reflection;

namespace Jellyfin.Plugin.BetterFiltering.Client;

/// <summary>
/// Reads the embedded client script.
/// </summary>
/// <remarks>
/// The script is embedded in the assembly rather than copied next to it. The plugin folder
/// is replaced wholesale on install, and <c>prototype/deploy-plugin.sh</c> sweeps only
/// <c>*.dll</c> and <c>*.json</c>; a loose <c>.js</c> file would survive that sweep and go
/// stale across upgrades, which is the exact class of bug this project exists to remove.
/// </remarks>
public sealed class ClientScriptProvider
{
    private const string ResourceSuffix = ".intersect-client.js";

    private readonly Lazy<string> _script;

    /// <summary>
    /// Initializes a new instance of the <see cref="ClientScriptProvider"/> class.
    /// </summary>
    public ClientScriptProvider()
    {
        _script = new Lazy<string>(Read, LazyThreadSafetyMode.ExecutionAndPublication);
    }

    /// <summary>
    /// Gets the script body, without any surrounding tags.
    /// </summary>
    public string Script => _script.Value;

    private static string Read()
    {
        var assembly = typeof(ClientScriptProvider).Assembly;

        // Matched by suffix rather than by a computed name: the default resource name is
        // derived from the root namespace and folder layout, so writing it out literally
        // would silently break the day either changes.
        var name = Array.Find(
            assembly.GetManifestResourceNames(),
            n => n.EndsWith(ResourceSuffix, StringComparison.Ordinal));

        if (name is null)
        {
            throw new InvalidOperationException(
                $"The embedded client script ('*{ResourceSuffix}') is missing from "
                + $"{assembly.GetName().Name}. The build did not embed it, so no client UI "
                + "can be injected; check the EmbeddedResource item in the .csproj.");
        }

        using var stream = assembly.GetManifestResourceStream(name)!;
        using var reader = new StreamReader(stream);
        return reader.ReadToEnd();
    }
}
