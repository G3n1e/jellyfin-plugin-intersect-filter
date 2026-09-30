using System.Net.Mime;
using Jellyfin.Data.Enums;
using Jellyfin.Database.Implementations.Entities;
using Jellyfin.Database.Implementations.Enums;
using Jellyfin.Plugin.BetterFiltering.Models;
using Jellyfin.Plugin.BetterFiltering.Services;
using MediaBrowser.Controller.Dto;
using MediaBrowser.Controller.Library;
using MediaBrowser.Model.Dto;
using MediaBrowser.Model.Querying;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Mvc;

namespace Jellyfin.Plugin.BetterFiltering.Controllers;

/// <summary>
/// Exposes the N-way intersection over HTTP.
/// </summary>
[ApiController]
[Authorize]
[Route("BetterJellyfinFiltering")]
[Produces(MediaTypeNames.Application.Json)]
public class IntersectController : ControllerBase
{
    /// <summary>
    /// The claim Jellyfin puts the authenticated user's id in.
    /// </summary>
    /// <remarks>
    /// Duplicated as a literal because it lives in <c>Jellyfin.Api</c>
    /// (Constants/InternalClaimTypes.cs), which is not published as a NuGet package and so
    /// cannot be referenced by a plugin. The string is part of the wire contract between the
    /// server and its clients, not an internal detail.
    /// </remarks>
    private const string UserIdClaim = "Jellyfin-UserId";

    private readonly GenreIntersectionService _service;
    private readonly IUserManager _userManager;
    private readonly IDtoService _dtoService;

    /// <summary>
    /// Initializes a new instance of the <see cref="IntersectController"/> class.
    /// </summary>
    /// <param name="service">The intersection service.</param>
    /// <param name="userManager">Instance of the <see cref="IUserManager"/> interface.</param>
    /// <param name="dtoService">Instance of the <see cref="IDtoService"/> interface.</param>
    public IntersectController(
        GenreIntersectionService service,
        IUserManager userManager,
        IDtoService dtoService)
    {
        _service = service;
        _userManager = userManager;
        _dtoService = dtoService;
    }

    /// <summary>
    /// Intersects any number of genre groups and returns one page of the result.
    /// </summary>
    /// <remarks>
    /// Repeat <c>groups</c> once per AND-group; separate names within a group with <c>|</c>
    /// to OR them, matching the stock meaning of <c>|</c> in <c>genres</c>. For example
    /// <c>?groups=Alpha|Beta&amp;groups=Gamma</c> means (Alpha OR Beta) AND Gamma, and
    /// <c>?groups=Alpha&amp;groups=Beta&amp;groups=Gamma</c> is a 3-way intersection that the
    /// stock API cannot express.
    /// </remarks>
    /// <param name="parentId">Library or folder to search. Omit for the whole library.</param>
    /// <param name="recursive">Whether to search recursively. Defaults to true.</param>
    /// <param name="includeItemTypes">Item types to include, comma separated. Empty means all.</param>
    /// <param name="fields">Additional DTO fields to populate, comma separated.</param>
    /// <param name="startIndex">Page offset. Defaults to 0.</param>
    /// <param name="limit">Page size.</param>
    /// <param name="sortBy">Sort fields, comma separated.</param>
    /// <param name="sortOrder">Sort direction: Ascending or Descending.</param>
    /// <param name="userId">
    /// Only honoured for callers without a user binding (API keys). A signed-in caller always
    /// gets its own access restrictions and may not request another user's view.
    /// </param>
    /// <returns>One page of the intersection, with its exact total.</returns>
    [HttpGet("Intersect")]
    public ActionResult<IntersectResponse> Intersect(
        [FromQuery] Guid? parentId = null,
        [FromQuery] bool? recursive = null,
        [FromQuery] string? includeItemTypes = null,
        [FromQuery] string? fields = null,
        [FromQuery] int? startIndex = null,
        [FromQuery] int? limit = null,
        [FromQuery] string? sortBy = null,
        [FromQuery] string? sortOrder = null,
        [FromQuery] Guid? userId = null)
    {
        var configuration = Plugin.Instance?.Configuration ?? new PluginConfiguration();
        var errors = new List<string>();

        // Read `groups` straight off the query collection, one entry per repetition.
        // Binding it as string[] would let the default binder split each value on commas and
        // silently destroy the AND/OR structure - see GroupParser's remarks.
        var groups = GroupParser.Parse(Request.Query["groups"].ToArray());

        if (!groups.IsValid)
        {
            errors.AddRange(groups.Errors!);
        }

        var itemTypes = ParseEnumList<BaseItemKind>(includeItemTypes, "includeItemTypes", errors);
        var dtoFields = ParseEnumList<ItemFields>(fields, "fields", errors);
        var sortFields = ParseEnumList<ItemSortBy>(sortBy, "sortBy", errors);

        var order = SortOrder.Ascending;
        if (!string.IsNullOrWhiteSpace(sortOrder)
            && !Enum.TryParse(sortOrder, ignoreCase: true, out order))
        {
            errors.Add($"sortOrder '{sortOrder}' is not a valid SortOrder (Ascending or Descending).");
        }

        var pageSize = limit ?? configuration.DefaultPageSize;
        if (pageSize < 1 || pageSize > configuration.MaxPageSize)
        {
            errors.Add($"limit must be between 1 and {configuration.MaxPageSize} (got {pageSize}).");
        }

        var offset = startIndex ?? 0;
        if (offset < 0)
        {
            errors.Add($"startIndex must not be negative (got {offset}).");
        }

        if (errors.Count > 0)
        {
            // Fail loudly. Never degrade a malformed filter into an unfiltered query: that is
            // how the stock delimiter trap turns a typo into a silently wrong answer.
            return BadRequest(new ErrorResponse(errors));
        }

        var (user, userError) = ResolveUser(userId);
        if (userError is not null)
        {
            return StatusCode(StatusCodes.Status403Forbidden, new ErrorResponse([userError]));
        }

        var request = new IntersectionRequest(
            user,
            parentId ?? Guid.Empty,
            recursive ?? true,
            itemTypes,
            groups.Groups!,
            offset,
            pageSize,
            [.. sortFields.Select(f => (f, order))],
            new DtoOptions { Fields = dtoFields });

        try
        {
            var outcome = _service.Intersect(request, configuration);

            // Passing the user here means the DTO layer independently re-checks visibility.
            // Items reaching this point are already filtered, so this is redundant by design:
            // on an access-control boundary the redundant check is the one worth paying for.
            var items = _dtoService.GetBaseItemDtos(outcome.Items, request.DtoOptions, user);

            return Ok(new IntersectResponse(
                items,
                outcome.TotalRecordCount,
                offset,
                outcome.FilterApplied,
                [.. outcome.Criteria.Select(c => new CriterionResponse(c.Names, c.Matched, c.RunningIntersection))],
                outcome.FilterApplied
                    ? []
                    : ["No groups were supplied, so no criterion was applied and the unfiltered "
                       + "library was returned. An empty selection is treated as 'no filter', "
                       + "not 'match nothing'."]));
        }
        catch (UnknownParentException ex)
        {
            // A bad parentId is a client error. Reporting it keeps the scope from being dropped
            // silently, which would answer a question about one folder with the whole library.
            return BadRequest(new ErrorResponse([ex.Message]));
        }
        catch (CriterionTooLargeException ex)
        {
            return StatusCode(StatusCodes.Status422UnprocessableEntity, new ErrorResponse([ex.Message]));
        }
    }

    /// <summary>
    /// Resolves which user's access restrictions apply to this request.
    /// </summary>
    private (User? User, string? Error) ResolveUser(Guid? requestedUserId)
    {
        var claim = User.FindFirst(UserIdClaim)?.Value;

        if (Guid.TryParse(claim, out var callerId))
        {
            // A signed-in caller is always scoped to itself. Letting it name another user
            // would turn a filter endpoint into a way to read someone else's library.
            if (requestedUserId is not null && requestedUserId.Value != callerId)
            {
                return (null, "A signed-in caller may only query its own library; "
                              + "the userId parameter is not accepted here.");
            }

            return (_userManager.GetUserById(callerId), null);
        }

        // No user claim: an API key or other elevated caller. API keys are not user-bound
        // (docs/FINDINGS.md section 2), so a user must be named explicitly to get user-scoped
        // filtering, and none is applied otherwise.
        if (requestedUserId is not null)
        {
            return (_userManager.GetUserById(requestedUserId.Value), null);
        }

        return (null, null);
    }

    /// <summary>
    /// Parses a comma-separated list of enum names, reporting unknown values instead of dropping them.
    /// </summary>
    private static TEnum[] ParseEnumList<TEnum>(string? value, string parameterName, List<string> errors)
        where TEnum : struct, Enum
    {
        if (string.IsNullOrWhiteSpace(value))
        {
            return [];
        }

        var parts = value.Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
        var parsed = new List<TEnum>(parts.Length);

        foreach (var part in parts)
        {
            if (Enum.TryParse<TEnum>(part, ignoreCase: true, out var item))
            {
                parsed.Add(item);
            }
            else
            {
                // The stock binder drops unparseable values, which silently widens the
                // result set. Reporting it keeps a typo from changing the answer.
                errors.Add($"{parameterName} value '{part}' is not a valid {typeof(TEnum).Name}.");
            }
        }

        return [.. parsed];
    }
}

/// <summary>
/// The HTTP response for an intersection request.
/// </summary>
/// <param name="Items">One page of the result.</param>
/// <param name="TotalRecordCount">The exact size of the whole intersection.</param>
/// <param name="StartIndex">The page offset this page starts at.</param>
/// <param name="FilterApplied">Whether any criterion was applied.</param>
/// <param name="Criteria">Per-criterion diagnostics, in evaluation order.</param>
/// <param name="Warnings">Human-readable notes about anything surprising in this response.</param>
public sealed record IntersectResponse(
    IReadOnlyList<BaseItemDto> Items,
    int TotalRecordCount,
    int StartIndex,
    bool FilterApplied,
    IReadOnlyList<CriterionResponse> Criteria,
    IReadOnlyList<string> Warnings);

/// <summary>
/// Diagnostics for one criterion.
/// </summary>
/// <param name="Names">The genre names ORed together in this group.</param>
/// <param name="Matched">How many items matched this criterion on its own.</param>
/// <param name="RunningIntersection">The intersection size after applying this criterion.</param>
public sealed record CriterionResponse(string[] Names, int Matched, int RunningIntersection);

/// <summary>
/// A failure response.
/// </summary>
/// <param name="Errors">The reasons the request was refused.</param>
public sealed record ErrorResponse(IReadOnlyList<string> Errors);
