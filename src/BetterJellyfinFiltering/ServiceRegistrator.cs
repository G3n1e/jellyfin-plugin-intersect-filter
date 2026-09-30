using Jellyfin.Plugin.BetterFiltering.Services;
using MediaBrowser.Controller;
using MediaBrowser.Controller.Plugins;
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
    }
}
