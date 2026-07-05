namespace Lexicast.UI.Pages;

/// <summary>
/// Data handed from <see cref="UploadPage"/> to <see cref="ProgressPage"/> via Frame navigation.
/// </summary>
public sealed record ProgressPageParameter(string JobId, string SourceFileName);
