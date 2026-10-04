using Jellyfin.Plugin.BetterFiltering.Client;
using Jellyfin.Plugin.BetterFiltering.Services;
using MediaBrowser.Controller;
using MediaBrowser.Controller.Plugins;
using Microsoft.AspNetCore.Hosting;
using Microsoft.Extensions.DependencyInjection;

namespace Jellyfin.Plugin.BetterFiltering;

/// <summary>
/// Registers the plugin's services with the server's container.
/// </summary>
/// <remarks>
/// The plugin's controller is discovered automatically (the server adds any plugin assembly
/// containing a <c>ControllerBase</c> subclass as an MVC application part), but the services
/// it depends on are not, so they are registered here.
/// </remarks>
public class ServiceRegistrator : IPluginServiceRegistrator
{
    /// <inheritdoc />
    public void RegisterServices(IServiceCollection serviceCollection, IServerApplicationHost applicationHost)
    {
        ArgumentNullException.ThrowIfNull(serviceCollection);

        serviceCollection.AddSingleton<GenreIntersectionService>();
        serviceCollection.AddSingleton<ClientScriptProvider>();

        // The server's own pipeline serves the web client's index.html, and a plugin has no
        // other hook into it, so the script is spliced into that response as it goes out.
        // Registered as an IStartupFilter so it wraps the whole pipeline and therefore runs
        // on the way out of the static-file handler, which is where the body exists.
        serviceCollection.AddSingleton<IStartupFilter, ScriptInjectingStartupFilter>();
    }
}
