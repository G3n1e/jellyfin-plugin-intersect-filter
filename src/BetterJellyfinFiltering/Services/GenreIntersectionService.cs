using Jellyfin.Data.Enums;
using Jellyfin.Database.Implementations.Entities;
using Jellyfin.Database.Implementations.Enums;
using Jellyfin.Extensions;
using MediaBrowser.Controller.Dto;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Entities.Movies;
using MediaBrowser.Controller.Library;
using MediaBrowser.Controller.Persistence;
using MediaBrowser.Controller.Playlists;
using MediaBrowser.Model.Querying;

namespace Jellyfin.Plugin.BetterFiltering.Services;

/// <summary>
/// Performs the N-way intersection that the stock <c>/Items</c> query cannot express.
/// </summary>
/// <remarks>
/// <para>
/// Stock <c>/Items</c> ANDs across exactly two genre groups (<c>genres</c>, pipe-delimited
/// names; <c>genreIds</c>, comma-delimited GUIDs), so <c>(A|B) AND (C|D)</c> is the ceiling
/// and <c>A AND B AND C</c> is not expressible (docs/FINDINGS.md section 2). This service
/// removes that ceiling for an arbitrary number of groups.
/// </para>
/// <para>
/// <b>Why this uses <see cref="IItemRepository"/> and one query per criterion, rather than
/// a single hand-written EF Core query.</b> A query written directly against
/// <c>BaseItemEntity</c>/<c>ItemValueMap</c> would have to re-derive every semantic the
/// normal query pipeline already encodes - placeholder exclusion, genre matching, item-type
/// filters, virtual-item handling and user rating/tag restrictions. Re-deriving those by hand
/// is precisely the "looks plausible, is wrong" failure this project exists to eliminate, so
/// each criterion is evaluated by the real query pipeline and only the set intersection is
/// done here. Intersecting correctly-computed sets is correct by construction;
/// re-implementing the filters would not be.
/// </para>
/// <para>
/// The one exception is the parent scope, which the server normally rewrites above the
/// repository layer. See <see cref="ApplyParentScope"/> for why that rewrite is mirrored here
/// and how it is kept honest.
/// </para>
/// </remarks>
public sealed class GenreIntersectionService
{
    private readonly IItemRepository _repository;
    private readonly ILibraryManager _libraryManager;

    /// <summary>
    /// Initializes a new instance of the <see cref="GenreIntersectionService"/> class.
    /// </summary>
    /// <param name="repository">Instance of the <see cref="IItemRepository"/> interface.</param>
    /// <param name="libraryManager">Instance of the <see cref="ILibraryManager"/> interface.</param>
    public GenreIntersectionService(IItemRepository repository, ILibraryManager libraryManager)
    {
        _repository = repository;
        _libraryManager = libraryManager;
    }

    /// <summary>
    /// Resolves the intersection of the requested criterion groups and returns one page of it.
    /// </summary>
    /// <param name="request">The resolved request.</param>
    /// <param name="configuration">The plugin configuration.</param>
    /// <returns>The page, its exact total, and per-criterion diagnostics.</returns>
    /// <exception cref="CriterionTooLargeException">
    /// A criterion matched more ids than <see cref="PluginConfiguration.MaxCriterionIds"/>.
    /// </exception>
    public IntersectionOutcome Intersect(IntersectionRequest request, PluginConfiguration configuration)
    {
        ArgumentNullException.ThrowIfNull(request);
        ArgumentNullException.ThrowIfNull(configuration);

        // ---- Zero groups means "no filter applied", not "match nothing".
        //
        // The kickoff (section 7) warns that in Jellyfin an empty list conventionally means
        // "all" rather than "none", and the query layer makes that concrete for ItemIds: it
        // guards `if (filter.ItemIds.Length > 0)`, so an empty array applies no filter at all.
        // Treating an empty selection as "show nothing" would be the surprising reading, so
        // the library is returned unfiltered - and the response records that explicitly
        // (FilterApplied = false) so a client cannot mistake it for a filtered result.
        if (request.Groups.Count == 0)
        {
            var unfiltered = ExecutePage(request, itemIds: null);
            return new IntersectionOutcome(unfiltered.Items, unfiltered.Total, FilterApplied: false, []);
        }

        var statistics = new List<CriterionStat>(request.Groups.Count);
        Guid[]? running = null;

        foreach (var names in request.Groups)
        {
            var matched = FetchCriterionIds(request, names, running, configuration);

            running = running is null ? matched : Intersect(running, matched);
            statistics.Add(new CriterionStat(names, matched.Length, running.Length));

            if (running.Length == 0)
            {
                // Empty is absorbing: no later criterion can make it non-empty, so stop
                // rather than issue queries whose outcome is already decided.
                break;
            }
        }

        var total = running!.Length;

        if (total == 0)
        {
            // Return WITHOUT touching the repository. Handing an empty id array to the
            // paging query would not match nothing - it would skip the id filter and return
            // the whole library while reporting an empty result
            // (BaseItemRepository.TranslateQuery.cs guards on `filter.ItemIds.Length > 0`).
            return new IntersectionOutcome([], 0, FilterApplied: true, statistics);
        }

        var page = ExecutePage(request, running);

        // The count is the size of the intersected set, not the page size: this is the
        // number the stock API cannot report, because it cannot express the query.
        return new IntersectionOutcome(page.Items, total, FilterApplied: true, statistics);
    }

    /// <summary>
    /// Fetches the complete id set for one criterion.
    /// </summary>
    private Guid[] FetchCriterionIds(
        IntersectionRequest request,
        string[] names,
        Guid[]? running,
        PluginConfiguration configuration)
    {
        var query = BuildBaseQuery(request);
        query.Genres = names;
        query.EnableTotalRecordCount = false;

        // Bound the read so a pathologically broad criterion is an explicit error instead of
        // an unbounded scan. Fetching ONE MORE than the cap is what makes an overflow
        // detectable, and an overflowing result is never used - so this does not reintroduce
        // the paging trap: a truncated set is reported, never intersected.
        query.Limit = configuration.MaxCriterionIds + 1;

        if (running is not null)
        {
            // Narrow this criterion to the running intersection. Intersection is associative,
            // so the result is identical, but every query after the first is then bounded by
            // the current intersection size rather than by the size of the genre. `running` is
            // never empty here - the caller breaks out before reaching this point.
            query.ItemIds = running;
        }

        // Deliberately no StartIndex: GetItemIdsList applies Skip/Take, so a StartIndex without
        // a Limit would return an unbounded tail. Paging a set before intersecting it is the
        // silent-wrong-answer bug recorded in docs/FINDINGS.md section 5 - a truncated genre
        // set intersects to a number that looks like a normal answer and is simply too small.
        var ids = _repository.GetItemIdsList(query);

        if (ids.Count > configuration.MaxCriterionIds)
        {
            throw new CriterionTooLargeException(names, configuration.MaxCriterionIds);
        }

        return [.. ids];
    }

    /// <summary>
    /// Materialises one page, either of the intersected id set or of the unfiltered library.
    /// </summary>
    private (IReadOnlyList<BaseItem> Items, int Total) ExecutePage(IntersectionRequest request, Guid[]? itemIds)
    {
        var query = BuildBaseQuery(request);
        query.OrderBy = request.OrderBy;
        query.StartIndex = request.StartIndex;
        query.Limit = request.Limit;

        if (itemIds is not null)
        {
            if (itemIds.Length == 0)
            {
                // Unreachable by construction; asserted because the failure mode is a silent
                // "return the entire library" rather than an exception.
                throw new InvalidOperationException(
                    "Refusing to page an empty id set: an empty ItemIds disables the id filter " +
                    "instead of matching nothing.");
            }

            query.ItemIds = itemIds;

            // The exact count is already known from the intersection, and it is the only
            // correct value here: the query's own count would be computed for this page.
            query.EnableTotalRecordCount = false;

            var page = _repository.GetItems(query);
            return (page.Items, itemIds.Length);
        }

        var unfiltered = _repository.GetItems(query);
        return (unfiltered.Items, unfiltered.TotalRecordCount);
    }

    /// <summary>
    /// Builds the query shared by every criterion and by the final page.
    /// </summary>
    private InternalItemsQuery BuildBaseQuery(IntersectionRequest request)
    {
        var query = new InternalItemsQuery
        {
            // Recursive is not read by the repository itself - it only means something inside
            // LibraryManager, which expands a recursive parent into an ancestor or top-parent
            // scope (see ApplyParentScope). It is still set, so the query describes itself.
            Recursive = request.Recursive,
            IncludeItemTypes = request.IncludeItemTypes,
            DtoOptions = request.DtoOptions,
        };

        ApplyParentScope(query, request);

        if (request.User is not null)
        {
            // Applied through the library manager rather than InternalItemsQuery.SetUser
            // directly, because the two are not equivalent. SetUser handles the rating limit,
            // blocked unrated items and blocked tags; the library manager additionally scopes an
            // otherwise-unscoped query to the libraries this user may see
            // (LibraryManager.AddUserToQuery). Calling SetUser alone filters by rating and tags
            // while leaving every library in the result.
            _libraryManager.ConfigureUserAccess(query, request.User);
        }

        return query;
    }

    /// <summary>
    /// Applies the parent scope to a query, reproducing the stock
    /// <c>/Items?parentId=...&amp;recursive=true</c> semantics.
    /// </summary>
    /// <remarks>
    /// <para>
    /// This is the one place the service restates server behaviour instead of calling into it,
    /// and the reason is worth being precise about.
    /// <see cref="InternalItemsQuery.ParentId"/> does <b>not</b> mean "everything under this
    /// folder". At the repository level it translates to a direct-parent predicate
    /// (<c>BaseItemRepository.TranslateQuery.cs</c>: <c>e.ParentId!.Value == filter.ParentId</c>),
    /// and <see cref="InternalItemsQuery.Recursive"/> is never read there at all. The stock
    /// route works only because <c>LibraryManager</c> rewrites the query on the way down,
    /// turning a recursive parent into <see cref="InternalItemsQuery.AncestorIds"/> or
    /// <see cref="InternalItemsQuery.TopParentIds"/>.
    /// </para>
    /// <para>
    /// Setting <c>ParentId</c> and handing the query straight to the repository - the obvious
    /// reading, and what this service did first - silently returns only the folder's immediate
    /// children. Against a library whose movies sit one level deeper, that is an empty result
    /// that looks like a legitimate "no matches" (docs/FINDINGS.md section 8). The branches
    /// below mirror <c>LibraryManager.SetTopParentIdsOrAncestors</c> one for one.
    /// </para>
    /// <para>
    /// The alternative - routing through <see cref="ILibraryManager.GetItemList"/> so the server
    /// performs the rewrite - was rejected because it materialises <see cref="BaseItem"/> objects
    /// rather than ids, and these queries are deliberately unbounded: a criterion must see its
    /// whole genre, not a page of it. Materialising a genre's worth of items per criterion is not
    /// affordable memory or time. The mirror is instead checked against the stock route on real
    /// data by prototype/server-controller-test.py, so a drift from it fails a test rather than
    /// shipping.
    /// </para>
    /// <para>
    /// Known divergence: for a <c>UserView</c> belonging to a user with grouped folders, the
    /// server resolves the grouping preference and this does not, so such a parent falls through
    /// to the view's own ancestor id. Grouped folders are a display preference rather than a
    /// filter, and library ids are the ordinary parent here, so this is recorded rather than
    /// worked around.
    /// </para>
    /// </remarks>
    private void ApplyParentScope(InternalItemsQuery query, IntersectionRequest request)
    {
        if (request.ParentId.IsEmpty())
        {
            // No parent: the whole library. Any scoping that applies comes from
            // ConfigureUserAccess.
            return;
        }

        if (!request.Recursive)
        {
            // Non-recursive is exactly what the repository's ParentId means: direct children.
            // The stock route also post-filters children in memory through UserViewBuilder,
            // which differs only for virtual and user-specific items.
            query.ParentId = request.ParentId;
            return;
        }

        var parent = _libraryManager.GetItemById(request.ParentId)
            ?? throw new UnknownParentException(request.ParentId);

        if (parent is CollectionFolder collectionFolder && collectionFolder.PhysicalFolderIds.Length > 0)
        {
            // A library is optimised to its physical folders rather than to its own id, because
            // BaseItemEntity.TopParentId is a column the repository can filter on directly.
            query.TopParentIds = collectionFolder.PhysicalFolderIds;
            return;
        }

        if (parent is Folder folder
            && folder is Playlist or BoxSet
            && folder.LinkedChildren.Length > 0)
        {
            // Playlists and BoxSets keep their contents in LinkedChildren and never write
            // ancestor rows for those items, so an ancestor query over one returns nothing.
            query.DescendantOfId = folder.Id;
            return;
        }

        // Everything else - a plain folder, a Series, a Season - scopes by ancestor id.
        // AncestorIds is a closure in the database (one row per ancestor, not merely the
        // parent), so a single id matches at any depth below it.
        var ancestorIds = parent.GetIdsForAncestorQuery().ToArray();
        if (ancestorIds.Length == 0)
        {
            // Never leave the scope empty. At the repository level an empty id set is not a
            // filter that matches nothing, it is no filter at all - which would widen the query
            // to every library instead of narrowing it to none.
            ancestorIds = [Guid.NewGuid()];
        }

        query.AncestorIds = ancestorIds;
    }

    /// <summary>
    /// Intersects two id sets, preserving the order of <paramref name="left"/>.
    /// </summary>
    private static Guid[] Intersect(Guid[] left, IReadOnlyList<Guid> right)
    {
        if (right.Count == 0 || left.Length == 0)
        {
            return [];
        }

        var rightSet = new HashSet<Guid>(right);
        var result = new List<Guid>(Math.Min(left.Length, right.Count));

        foreach (var id in left)
        {
            if (rightSet.Contains(id))
            {
                result.Add(id);
            }
        }

        return [.. result];
    }
}

/// <summary>
/// A resolved intersection request.
/// </summary>
/// <param name="User">The user whose access restrictions apply, if any.</param>
/// <param name="ParentId">The library or folder to search, or <see cref="Guid.Empty"/> for the whole library.</param>
/// <param name="Recursive">Whether to search recursively.</param>
/// <param name="IncludeItemTypes">Item types to include; empty means no type restriction.</param>
/// <param name="Groups">The AND-groups; names within a group are ORed.</param>
/// <param name="StartIndex">The page offset.</param>
/// <param name="Limit">The page size.</param>
/// <param name="OrderBy">Sort fields with their directions.</param>
/// <param name="DtoOptions">DTO shaping options.</param>
public sealed record IntersectionRequest(
    User? User,
    Guid ParentId,
    bool Recursive,
    BaseItemKind[] IncludeItemTypes,
    IReadOnlyList<string[]> Groups,
    int StartIndex,
    int Limit,
    IReadOnlyList<(ItemSortBy OrderBy, SortOrder SortOrder)> OrderBy,
    DtoOptions DtoOptions);

/// <summary>
/// Diagnostics for one criterion, in evaluation order.
/// </summary>
/// <param name="Names">The genre names ORed together in this group.</param>
/// <param name="Matched">How many items matched this criterion on its own.</param>
/// <param name="RunningIntersection">The intersection size after applying this criterion.</param>
public sealed record CriterionStat(string[] Names, int Matched, int RunningIntersection);

/// <summary>
/// The result of an intersection.
/// </summary>
/// <param name="Items">One page of the intersection.</param>
/// <param name="TotalRecordCount">The exact size of the whole intersection.</param>
/// <param name="FilterApplied">Whether any criterion was applied at all.</param>
/// <param name="Criteria">Per-criterion diagnostics.</param>
public sealed record IntersectionOutcome(
    IReadOnlyList<BaseItem> Items,
    int TotalRecordCount,
    bool FilterApplied,
    IReadOnlyList<CriterionStat> Criteria);

/// <summary>
/// Thrown when a request names a parent folder that does not exist.
/// </summary>
/// <remarks>
/// Raised rather than ignored because the alternative at this layer is to drop the scope
/// silently, which turns a typo in a folder id into a query over the entire library.
/// </remarks>
public sealed class UnknownParentException : Exception
{
    /// <summary>
    /// Initializes a new instance of the <see cref="UnknownParentException"/> class.
    /// </summary>
    /// <param name="parentId">The id that could not be resolved.</param>
    public UnknownParentException(Guid parentId)
        : base($"No item exists with id {parentId}. The parent folder could not be resolved.")
    {
        ParentItemId = parentId;
    }

    /// <summary>
    /// Gets the id that could not be resolved.
    /// </summary>
    public Guid ParentItemId { get; }
}

/// <summary>
/// Thrown when a single criterion matches more ids than the configured cap allows.
/// </summary>
public sealed class CriterionTooLargeException : Exception
{
    /// <summary>
    /// Initializes a new instance of the <see cref="CriterionTooLargeException"/> class.
    /// </summary>
    /// <param name="names">The genre names in the criterion.</param>
    /// <param name="cap">The configured cap.</param>
    public CriterionTooLargeException(IReadOnlyList<string> names, int cap)
        : base($"Criterion '{string.Join("|", names)}' matched more than {cap} items. "
               + "Raise MaxCriterionIds in the plugin configuration, or narrow the criterion. "
               + "The result is refused rather than truncated because a truncated set that is "
               + "intersected produces a plausible but incorrect answer.")
    {
        Names = names;
        Cap = cap;
    }

    /// <summary>
    /// Gets the genre names in the offending criterion.
    /// </summary>
    public IReadOnlyList<string> Names { get; }

    /// <summary>
    /// Gets the configured cap.
    /// </summary>
    public int Cap { get; }
}
