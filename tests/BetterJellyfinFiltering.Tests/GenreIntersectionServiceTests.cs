using Jellyfin.Data.Enums;
using Jellyfin.Plugin.BetterFiltering.Services;
using MediaBrowser.Controller.Dto;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Library;
using MediaBrowser.Controller.Persistence;
using MediaBrowser.Model.Querying;
using Moq;

namespace Jellyfin.Plugin.BetterFiltering.Tests;

/// <summary>
/// The intersection itself: AND across groups, OR within them, and the traps that make an
/// implementation of it look correct while returning the wrong answer.
/// </summary>
/// <remarks>
/// The repository is faked so that each check is about the service's own algebra and about
/// what it asks the query layer for. Whether the real query layer then returns the right
/// genre sets is a separate question, answered against a live server by
/// prototype/server-controller-test.py.
/// </remarks>
public class GenreIntersectionServiceTests
{
    private static readonly Guid AllThree = Guid.Parse("11111111-1111-1111-1111-111111111111");
    private static readonly Guid Ab1 = Guid.Parse("22222222-2222-2222-2222-222222222222");
    private static readonly Guid Ab2 = Guid.Parse("33333333-3333-3333-3333-333333333333");
    private static readonly Guid Ag = Guid.Parse("44444444-4444-4444-4444-444444444444");
    private static readonly Guid Bg = Guid.Parse("55555555-5555-5555-5555-555555555555");
    private static readonly Guid Gd = Guid.Parse("66666666-6666-6666-6666-666666666666");

    /// <summary>
    /// The fixture library from prototype/make-fixtures.sh: three overlapping genres whose
    /// three-way intersection is a single item.
    /// </summary>
    private static FakeLibrary FixtureLibrary()
        => new FakeLibrary()
            .WithGenre("Alpha", AllThree, Ab1, Ab2, Ag)
            .WithGenre("Beta", AllThree, Ab1, Ab2, Bg)
            .WithGenre("Gamma", AllThree, Ag, Bg, Gd)
            .WithGenre("Delta", Gd)
            .WithLibrary(AllThree, Ab1, Ab2, Ag, Bg, Gd);

    /// <summary>
    /// Builds a request from groups that are already parsed: one <c>string[]</c> per AND-group,
    /// one entry per genre name. Splitting <c>"Alpha|Beta"</c> into two names is
    /// <see cref="Models.GroupParser"/>'s job and is covered by <see cref="GroupParserTests"/>;
    /// by the time a request reaches this service the pipes are gone.
    /// </summary>
    private static IntersectionRequest Request(
        IReadOnlyList<string[]> groups,
        Guid parentId = default,
        bool recursive = true,
        int startIndex = 0,
        int limit = 100)
        => new(
            null,
            parentId,
            recursive,
            [BaseItemKind.Movie],
            groups,
            startIndex,
            limit,
            [],
            new DtoOptions());

    private static Guid[] PageIds(IntersectionOutcome outcome)
        => [.. outcome.Items.Select(i => i.Id)];

    // ---- the thesis ------------------------------------------------------

    [Fact]
    public void ThreeGroups_Intersect_TheyDoNotUnion()
    {
        var library = FixtureLibrary();

        var outcome = library.Service.Intersect(Request([["Alpha"], ["Beta"], ["Gamma"]]), new PluginConfiguration());

        // The union of the same three genres is all six items; the stock API can only produce
        // the union, which is the gap this plugin exists to close.
        Assert.Equal(1, outcome.TotalRecordCount);
        Assert.Equal([AllThree], PageIds(outcome));
    }

    [Theory]
    [InlineData("Alpha", "Beta", 3)]
    [InlineData("Alpha", "Gamma", 2)]
    [InlineData("Beta", "Gamma", 2)]
    [InlineData("Alpha", "Delta", 0)]
    public void PairwiseIntersections_MatchTheExpectedSets(string a, string b, int expected)
    {
        var library = FixtureLibrary();

        var outcome = library.Service.Intersect(Request([[a], [b]]), new PluginConfiguration());

        Assert.Equal(expected, outcome.TotalRecordCount);
    }

    [Fact]
    public void NamesWithinAGroup_AreOred()
    {
        var library = FixtureLibrary();

        var outcome = library.Service.Intersect(Request([["Alpha", "Beta"]]), new PluginConfiguration());

        // Alpha OR Beta, with no second group to AND against.
        Assert.Equal(5, outcome.TotalRecordCount);
    }

    [Fact]
    public void OrWithinAnd_IsTheStockCeilingCase()
    {
        var library = FixtureLibrary();

        var outcome = library.Service.Intersect(Request([["Alpha", "Beta"], ["Gamma"]]), new PluginConfiguration());

        // (Alpha OR Beta) AND Gamma = {All Three, Alpha Gamma, Beta Gamma}
        Assert.Equal(3, outcome.TotalRecordCount);
    }

    [Fact]
    public void CriterionOrder_DoesNotChangeTheResult()
    {
        var library = FixtureLibrary();

        var forward = library.Service.Intersect(Request([["Alpha"], ["Beta"], ["Gamma"]]), new PluginConfiguration());
        var reverse = library.Service.Intersect(Request([["Gamma"], ["Beta"], ["Alpha"]]), new PluginConfiguration());

        Assert.Equal(forward.TotalRecordCount, reverse.TotalRecordCount);
        Assert.Equal(PageIds(forward), PageIds(reverse));
    }

    // ---- the empty-list-means-all trap -----------------------------------

    [Fact]
    public void NoGroups_MeansNoFilter_NotMatchNothing()
    {
        var library = FixtureLibrary();

        var outcome = library.Service.Intersect(Request([]), new PluginConfiguration());

        Assert.False(outcome.FilterApplied);
        Assert.Empty(outcome.Criteria);

        // The whole library, not nothing.
        Assert.Equal(6, outcome.TotalRecordCount);

        // And no criterion was ever evaluated.
        Assert.Empty(library.CriterionQueries);
    }

    [Fact]
    public void EmptyIntersection_ReturnsNothing_WithoutEverAskingForItems()
    {
        var library = FixtureLibrary();

        var outcome = library.Service.Intersect(
            Request([["Alpha"], ["Beta"], ["Gamma"], ["Delta"]]),
            new PluginConfiguration());

        Assert.Equal(0, outcome.TotalRecordCount);
        Assert.Empty(outcome.Items);

        // Still a filtered result, not an absent one - a client must be able to tell "the
        // filter matched nothing" from "no filter was applied".
        Assert.True(outcome.FilterApplied);

        // THE landmine. The item query applies `filter.ItemIds` only when it is non-empty, so
        // handing it an empty array would return the entire library while reporting zero. The
        // service must therefore never issue that query at all.
        Assert.Empty(library.ItemQueries);
    }

    [Fact]
    public void EveryItemQuery_CarriesANonEmptyIdSetOrNoFilterAtAll()
    {
        // The general statement of the trap above, asserted over every path that reaches the
        // item query, so a future refactor cannot reintroduce it on just one branch.
        var library = FixtureLibrary();

        library.Service.Intersect(Request([["Alpha"], ["Beta"]]), new PluginConfiguration());
        library.Service.Intersect(Request([]), new PluginConfiguration());
        library.Service.Intersect(Request([["Alpha"], ["Delta"]]), new PluginConfiguration());

        // Three requests: a real intersection, no filter at all, and an intersection that came
        // out empty. Only the first and second reach the item query. Only the first carries an
        // id filter - the second is the unfiltered case, where an absent filter is the whole
        // point - and the third, which is the dangerous one, is never asked.
        Assert.Equal(2, library.ItemQueries.Count);
        Assert.Single(library.ItemQueries, q => q.ItemIds.Length > 0);
        Assert.Single(library.ItemQueries, q => q.ItemIds.Length == 0);
    }

    // ---- pagination bounds -----------------------------------------------

    [Fact]
    public void Page_ReportsTheWholeIntersection_NotThePageSize()
    {
        var library = FixtureLibrary();

        var outcome = library.Service.Intersect(
            Request([["Alpha"], ["Beta"]], startIndex: 0, limit: 1),
            new PluginConfiguration());

        Assert.Single(outcome.Items);
        Assert.Equal(3, outcome.TotalRecordCount);
    }

    [Fact]
    public void Paging_OverTheIntersection_YieldsEachItemExactlyOnce()
    {
        var library = FixtureLibrary();

        var seen = new List<Guid>();
        for (var i = 0; i < 3; i++)
        {
            var page = library.Service.Intersect(
                Request([["Alpha"], ["Beta"]], startIndex: i, limit: 1),
                new PluginConfiguration());

            Assert.Equal(3, page.TotalRecordCount);
            Assert.Single(page.Items);
            seen.AddRange(PageIds(page));
        }

        Assert.Equal(3, seen.Distinct().Count());
    }

    [Fact]
    public void Pagination_IsNotPushedDownIntoTheCriteria()
    {
        // A criterion must see its whole genre. Paging it first would intersect a truncated
        // set and produce a number that looks like a plausible answer and is simply too small.
        var library = FixtureLibrary();

        library.Service.Intersect(
            Request([["Alpha"], ["Beta"]], startIndex: 2, limit: 1),
            new PluginConfiguration());

        Assert.All(library.CriterionQueries, q => Assert.Null(q.StartIndex));
    }

    [Fact]
    public void LaterCriteria_AreNarrowedByTheRunningIntersection()
    {
        // Not required for correctness - intersection is associative - but it bounds every
        // query after the first by the current intersection rather than by the genre.
        var library = FixtureLibrary();

        library.Service.Intersect(Request([["Alpha"], ["Beta"], ["Gamma"]]), new PluginConfiguration());

        Assert.Equal(3, library.CriterionQueries.Count);
        Assert.Empty(library.CriterionQueries[0].ItemIds);

        var afterAlpha = library.CriterionQueries[1].ItemIds;
        Assert.Equal(4, afterAlpha.Length);

        var afterAlphaBeta = library.CriterionQueries[2].ItemIds;
        Assert.Equal(3, afterAlphaBeta.Length);
    }

    // ---- diagnostics -----------------------------------------------------

    [Fact]
    public void Criteria_ReportEachCriterionAndTheNarrowingItCaused()
    {
        var library = FixtureLibrary();

        var outcome = library.Service.Intersect(Request([["Alpha"], ["Beta"], ["Gamma"]]), new PluginConfiguration());

        Assert.Collection(
            outcome.Criteria,
            c => Assert.Equal((4, 4), (c.Matched, c.RunningIntersection)),
            c => Assert.Equal((3, 3), (c.Matched, c.RunningIntersection)),
            c => Assert.Equal((1, 1), (c.Matched, c.RunningIntersection)));
    }

    [Fact]
    public void AnEmptyIntersection_StopsEvaluatingFurtherCriteria()
    {
        var library = FixtureLibrary();

        library.Service.Intersect(
            Request([["Alpha"], ["Delta"], ["Beta"], ["Gamma"]]),
            new PluginConfiguration());

        // Alpha AND Delta is already empty, and empty is absorbing, so the remaining criteria
        // are decided before they are asked.
        Assert.Equal(2, library.CriterionQueries.Count);
    }

    // ---- the criterion cap -----------------------------------------------

    [Fact]
    public void ACriterionOverTheCap_IsRefusedRatherThanTruncated()
    {
        var library = FixtureLibrary();
        var configuration = new PluginConfiguration { MaxCriterionIds = 3 };

        // Alpha matches 4 items with a cap of 3. Truncating to 3 and intersecting would give an
        // answer that is simply too small and looks entirely normal.
        var ex = Assert.Throws<CriterionTooLargeException>(
            () => library.Service.Intersect(Request([["Alpha"]]), configuration));

        Assert.Equal(["Alpha"], ex.Names);
        Assert.Equal(3, ex.Cap);
    }

    [Fact]
    public void ACriterionAtTheCap_IsAccepted()
    {
        var library = FixtureLibrary();

        var outcome = library.Service.Intersect(
            Request([["Alpha"]]),
            new PluginConfiguration { MaxCriterionIds = 4 });

        Assert.Equal(4, outcome.TotalRecordCount);
    }

    // ---- parent scoping --------------------------------------------------

    [Fact]
    public void RecursiveFolderParent_ScopesByAncestorId_NotByDirectParent()
    {
        // The bug this test exists for: InternalItemsQuery.ParentId means *direct children* at
        // the repository level, and Recursive is not read there at all. Setting ParentId for a
        // recursive folder query therefore returns only the folder's immediate children - for
        // a library whose movies sit a level deeper, an empty result that looks like a
        // legitimate "no matches".
        var folderId = Guid.NewGuid();
        var library = FixtureLibrary().WithFolder(folderId);

        library.Service.Intersect(Request([["Alpha"]], parentId: folderId), new PluginConfiguration());

        Assert.All(library.CriterionQueries, q =>
        {
            Assert.Equal([folderId], q.AncestorIds);
            Assert.Equal(Guid.Empty, q.ParentId);
        });
    }

    [Fact]
    public void NonRecursiveParent_IsADirectChildQuery()
    {
        var folderId = Guid.NewGuid();
        var library = FixtureLibrary().WithFolder(folderId);

        library.Service.Intersect(
            Request([["Alpha"]], parentId: folderId, recursive: false),
            new PluginConfiguration());

        Assert.All(library.CriterionQueries, q =>
        {
            Assert.Equal(folderId, q.ParentId);
            Assert.Empty(q.AncestorIds);
        });
    }

    [Fact]
    public void NoParent_LeavesTheQueryUnscoped()
    {
        var library = FixtureLibrary();

        library.Service.Intersect(Request([["Alpha"]]), new PluginConfiguration());

        Assert.All(library.CriterionQueries, q =>
        {
            Assert.Empty(q.AncestorIds);
            Assert.Empty(q.TopParentIds);
            Assert.Equal(Guid.Empty, q.ParentId);
        });
    }

    [Fact]
    public void UnknownParent_IsAnError_NotASilentlyDroppedScope()
    {
        var library = FixtureLibrary();

        var ex = Assert.Throws<UnknownParentException>(
            () => library.Service.Intersect(Request([["Alpha"]], parentId: Guid.NewGuid()), new PluginConfiguration()));

        // Dropping an unresolvable scope would answer a question about one folder with the
        // whole library.
        Assert.NotEqual(Guid.Empty, ex.ParentItemId);
    }

    [Fact]
    public void ThePageQuery_CarriesTheSameScopeAsTheCriteria()
    {
        var folderId = Guid.NewGuid();
        var library = FixtureLibrary().WithFolder(folderId);

        library.Service.Intersect(Request([["Alpha"], ["Beta"]], parentId: folderId), new PluginConfiguration());

        var page = Assert.Single(library.ItemQueries);
        Assert.Equal([folderId], page.AncestorIds);
    }

    // ---- the fake --------------------------------------------------------

    /// <summary>
    /// A stand-in for the query layer, holding an id per genre and answering both queries the
    /// service makes. Everything it returns is deliberately obvious; the point of these tests
    /// is what the service asks for and what it does with the answer.
    /// </summary>
    private sealed class FakeLibrary
    {
        private readonly Dictionary<string, Guid[]> _byGenre = new(StringComparer.Ordinal);
        private Guid[] _library = [];

        public FakeLibrary()
        {
            Repository
                .Setup(r => r.GetItemIdsList(It.IsAny<InternalItemsQuery>()))
                .Returns((InternalItemsQuery q) =>
                {
                    CriterionQueries.Add(q);

                    var allowed = q.ItemIds.Length == 0 ? null : q.ItemIds.ToHashSet();

                    return q.Genres
                        .SelectMany(g => _byGenre.TryGetValue(g, out var ids) ? ids : Array.Empty<Guid>())
                        .Distinct()
                        .Where(id => allowed is null || allowed.Contains(id))
                        .Take(q.Limit ?? int.MaxValue)
                        .ToArray();
                });

            Repository
                .Setup(r => r.GetItems(It.IsAny<InternalItemsQuery>()))
                .Returns((InternalItemsQuery q) =>
                {
                    ItemQueries.Add(q);

                    // An empty ItemIds is "no id filter" at this layer, exactly as it is in the
                    // real repository - which is why the service must never send one.
                    var universe = q.ItemIds.Length > 0 ? q.ItemIds : _library;

                    var page = universe
                        .Skip(q.StartIndex ?? 0)
                        .Take(q.Limit ?? int.MaxValue)
                        .Select(id => (BaseItem)new Folder { Id = id })
                        .ToArray();

                    return new QueryResult<BaseItem>(
                        q.StartIndex,
                        q.EnableTotalRecordCount ? universe.Length : null,
                        page);
                });

            // No parent resolves by default, so tests that pass one must declare it.
            LibraryManager
                .Setup(m => m.GetItemById(It.IsAny<Guid>()))
                .Returns((BaseItem?)null);
        }

        public Mock<IItemRepository> Repository { get; } = new();

        public Mock<ILibraryManager> LibraryManager { get; } = new();

        public List<InternalItemsQuery> CriterionQueries { get; } = [];

        public List<InternalItemsQuery> ItemQueries { get; } = [];

        public GenreIntersectionService Service => new(Repository.Object, LibraryManager.Object);

        public FakeLibrary WithGenre(string name, params Guid[] ids)
        {
            _byGenre[name] = ids;
            return this;
        }

        public FakeLibrary WithLibrary(params Guid[] ids)
        {
            _library = ids;
            return this;
        }

        /// <summary>
        /// Makes <paramref name="id"/> resolve to a plain folder, which is the case that has to
        /// scope by ancestor id.
        /// </summary>
        public FakeLibrary WithFolder(Guid id)
        {
            LibraryManager
                .Setup(m => m.GetItemById(id))
                .Returns(new Folder { Id = id });
            return this;
        }
    }
}
