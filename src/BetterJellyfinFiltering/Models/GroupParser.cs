namespace Jellyfin.Plugin.BetterFiltering.Models;

/// <summary>
/// Parses the <c>groups</c> query parameter into AND-groups of genre names.
/// </summary>
/// <remarks>
/// <para>
/// The wire format is deliberately NOT delegated to ASP.NET model binding.
/// Two separate hazards make the default binder unsafe here:
/// </para>
/// <list type="number">
/// <item><description>
/// ASP.NET Core splits an array-typed query parameter on <b>commas</b>. Declaring
/// the parameter as <c>string[] groups</c> would therefore flatten
/// <c>groups=A|B&amp;groups=C</c> in a way that loses the group boundaries —
/// silently converting an intersection into a union. The raw
/// <see cref="Microsoft.Extensions.Primitives.StringValues"/> are read instead,
/// so each repetition stays intact.
/// </description></item>
/// <item><description>
/// Jellyfin's own <c>CommaDelimitedCollectionModelBinder</c> catches
/// <c>FormatException</c>, logs at <b>Debug</b>, and drops the offending element
/// (Jellyfin.Api/ModelBinders/CommaDelimitedCollectionModelBinder.cs). A malformed
/// filter therefore degrades into <i>no filter at all</i> with no error — the
/// exact silent-wrong-answer failure this plugin exists to remove
/// (docs/FINDINGS.md section 2). Every malformed input here is a hard error.
/// </description></item>
/// </list>
/// <para>
/// Within one group the separator is <c>|</c>, matching Jellyfin's native
/// "pipe means OR" convention for <c>genres</c>. Groups are ANDed.
/// So <c>groups=Alpha|Beta&amp;groups=Gamma</c> means (Alpha OR Beta) AND Gamma.
/// </para>
/// </remarks>
public static class GroupParser
{
    /// <summary>
    /// Separator between genre names inside a single AND-group (OR within the group).
    /// </summary>
    public const char GroupSeparator = '|';

    /// <summary>
    /// Parses the raw repeated <c>groups</c> values.
    /// </summary>
    /// <param name="rawGroups">One entry per repetition of the query parameter.</param>
    /// <returns>The parsed groups, or the errors that prevent parsing.</returns>
    public static GroupParseResult Parse(IReadOnlyList<string?> rawGroups)
    {
        ArgumentNullException.ThrowIfNull(rawGroups);

        var errors = new List<string>();
        var groups = new List<string[]>(rawGroups.Count);

        for (var i = 0; i < rawGroups.Count; i++)
        {
            var raw = rawGroups[i] ?? string.Empty;

            // Empty entries are kept rather than dropped. Splitting with RemoveEmptyEntries
            // would quietly repair the typos: 'Alpha||Beta' would become 'Alpha|Beta' and
            // 'Alpha|' would become 'Alpha', each accepted as a well-formed filter the caller
            // never asked for. Repairing input silently is the same class of failure as
            // dropping it silently, so a blank name is reported instead.
            var names = raw.Split(GroupSeparator, StringSplitOptions.TrimEntries);
            var blank = Array.FindIndex(names, string.IsNullOrEmpty);

            if (blank >= 0)
            {
                errors.Add(names.Length == 1
                    ? $"groups[{i}] ('{raw}') contains no genre names. "
                      + $"Separate names within a group with '{GroupSeparator}'."
                    : $"groups[{i}] ('{raw}') has an empty genre name at position {blank}. "
                      + $"Remove the doubled or trailing '{GroupSeparator}'.");
                continue;
            }

            groups.Add(names);
        }

        return errors.Count > 0
            ? GroupParseResult.Invalid(errors)
            : GroupParseResult.Valid(groups);
    }
}

/// <summary>
/// Outcome of parsing the <c>groups</c> parameter.
/// </summary>
public sealed class GroupParseResult
{
    private GroupParseResult(IReadOnlyList<string[]>? groups, IReadOnlyList<string> errors)
    {
        Groups = groups;
        Errors = errors;
    }

    /// <summary>
    /// Gets the parsed AND-groups, or <see langword="null"/> when parsing failed.
    /// </summary>
    public IReadOnlyList<string[]>? Groups { get; }

    /// <summary>
    /// Gets the parse errors. Empty when parsing succeeded.
    /// </summary>
    public IReadOnlyList<string> Errors { get; }

    /// <summary>
    /// Gets a value indicating whether parsing succeeded.
    /// </summary>
    public bool IsValid => Groups is not null;

    /// <summary>
    /// Creates a successful result.
    /// </summary>
    /// <param name="groups">The parsed groups.</param>
    /// <returns>The result.</returns>
    public static GroupParseResult Valid(IReadOnlyList<string[]> groups) => new(groups, []);

    /// <summary>
    /// Creates a failed result.
    /// </summary>
    /// <param name="errors">The parse errors.</param>
    /// <returns>The result.</returns>
    public static GroupParseResult Invalid(IReadOnlyList<string> errors) => new(null, errors);
}
