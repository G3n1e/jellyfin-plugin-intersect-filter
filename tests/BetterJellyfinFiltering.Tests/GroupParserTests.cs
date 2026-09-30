using Jellyfin.Plugin.BetterFiltering.Models;

namespace Jellyfin.Plugin.BetterFiltering.Tests;

/// <summary>
/// The delimiter asymmetry: <c>|</c> ORs names inside one group, repeated parameters AND
/// groups. Every test here corresponds to a way the wire format can be misread.
/// </summary>
public class GroupParserTests
{
    [Fact]
    public void RepeatedParameters_StaySeparateGroups()
    {
        var result = GroupParser.Parse(["Alpha", "Beta", "Gamma"]);

        Assert.True(result.IsValid);
        Assert.Equal(3, result.Groups!.Count);
        Assert.All(result.Groups, g => Assert.Single(g));
    }

    [Fact]
    public void PipeWithinOneParameter_OrsNamesIntoASingleGroup()
    {
        // This is the asymmetry: the same three names, one parameter, one group of three.
        var result = GroupParser.Parse(["Alpha|Beta|Gamma"]);

        Assert.True(result.IsValid);
        var group = Assert.Single(result.Groups!);
        Assert.Equal(["Alpha", "Beta", "Gamma"], group);
    }

    [Fact]
    public void ParametersAndPipes_ComposeIntoAndOfOrs()
    {
        var result = GroupParser.Parse(["Alpha|Beta", "Gamma"]);

        Assert.True(result.IsValid);
        Assert.Equal(2, result.Groups!.Count);
        Assert.Equal(["Alpha", "Beta"], result.Groups[0]);
        Assert.Equal(["Gamma"], result.Groups[1]);
    }

    [Fact]
    public void CommaIsNotASeparator()
    {
        // The stock binder splits query values on commas, which would tear a group apart.
        // Reading the raw query collection is what keeps this name intact - and a name
        // containing a comma is a legitimate genre name, so it must not be split.
        var result = GroupParser.Parse(["Alpha,Beta"]);

        Assert.True(result.IsValid);
        var group = Assert.Single(result.Groups!);
        Assert.Equal(["Alpha,Beta"], group);
    }

    [Fact]
    public void NoParameters_YieldsNoGroups()
    {
        // Zero groups is the "no filter" case, and is valid: it is an empty *selection*, not
        // a malformed one. The service decides what it means.
        var result = GroupParser.Parse([]);

        Assert.True(result.IsValid);
        Assert.Empty(result.Groups!);
    }

    [Theory]
    [InlineData("")]
    [InlineData("|")]
    [InlineData("Alpha||Beta")]
    [InlineData("Alpha|")]
    [InlineData("|Alpha")]
    [InlineData("Alpha|||Beta")]
    public void EmptyNames_AreRejected_NotSilentlyRepaired(string raw)
    {
        // Splitting with RemoveEmptyEntries would turn "Alpha||Beta" into "Alpha|Beta" and
        // "Alpha|" into "Alpha" - accepting a filter the caller did not write. Repairing
        // malformed input silently is the same failure as dropping it silently.
        var result = GroupParser.Parse([raw]);

        Assert.False(result.IsValid);
        Assert.NotEmpty(result.Errors);
    }

    [Fact]
    public void EmptyNameError_NamesTheOffendingParameter()
    {
        var result = GroupParser.Parse(["Alpha", "Beta||Gamma"]);

        Assert.False(result.IsValid);
        var error = Assert.Single(result.Errors);
        Assert.Contains("groups[1]", error, StringComparison.Ordinal);
        Assert.Contains("Beta||Gamma", error, StringComparison.Ordinal);
    }

    [Fact]
    public void OneMalformedGroup_InvalidatesTheWholeRequest()
    {
        // Not a partial success: dropping the bad group and intersecting the rest would answer
        // a different question than the one asked, without saying so.
        var result = GroupParser.Parse(["Alpha", "", "Gamma"]);

        Assert.False(result.IsValid);
        Assert.Null(result.Groups);
    }

    [Fact]
    public void NamesAreTrimmed()
    {
        var result = GroupParser.Parse([" Alpha | Beta "]);

        Assert.True(result.IsValid);
        Assert.Equal(["Alpha", "Beta"], Assert.Single(result.Groups!));
    }

    [Fact]
    public void NullEntries_AreRejected()
    {
        var result = GroupParser.Parse([null]);

        Assert.False(result.IsValid);
        Assert.NotEmpty(result.Errors);
    }

    [Fact]
    public void GroupSeparatorIsPipe()
    {
        // Asserted because the whole wire format is defined by this one character, and the
        // client and the harness both encode it.
        Assert.Equal('|', GroupParser.GroupSeparator);
    }
}
