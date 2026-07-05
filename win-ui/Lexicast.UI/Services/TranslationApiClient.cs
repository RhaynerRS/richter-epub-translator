using System;
using System.Globalization;
using System.IO;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using Lexicast.UI.Models;

namespace Lexicast.UI.Services;

public sealed class TranslationApiClient
{
    private static readonly JsonSerializerOptions JsonOptions = new(JsonSerializerDefaults.Web);

    private readonly HttpClient _httpClient = new();

    public string BaseUrl { get; set; } = "http://localhost:8000";

    public async Task<TranslationJob> CreateTranslationAsync(
        string filePath,
        string targetLanguage,
        int concurrency,
        string? userPrompt,
        CancellationToken ct = default)
    {
        await using var fileStream = File.OpenRead(filePath);
        using var content = new MultipartFormDataContent();
        using var fileContent = new StreamContent(fileStream);
        fileContent.Headers.ContentType = new MediaTypeHeaderValue("application/epub+zip");
        content.Add(fileContent, "file", Path.GetFileName(filePath));
        content.Add(new StringContent(targetLanguage), "target_language");
        content.Add(new StringContent(concurrency.ToString(CultureInfo.InvariantCulture)), "concurrency");
        if (!string.IsNullOrWhiteSpace(userPrompt))
        {
            content.Add(new StringContent(userPrompt), "user_prompt");
        }

        using var response = await _httpClient.PostAsync(CombineUrl("/translations"), content, ct);
        await EnsureSuccessAsync(response, ct);
        return await ReadJsonAsync<TranslationJob>(response, ct);
    }

    public async Task<TranslationJob> GetJobAsync(string jobId, CancellationToken ct = default)
    {
        using var response = await _httpClient.GetAsync(CombineUrl($"/translations/{jobId}"), ct);
        await EnsureSuccessAsync(response, ct);
        return await ReadJsonAsync<TranslationJob>(response, ct);
    }

    public async Task DownloadAsync(string jobId, string destinationPath, CancellationToken ct = default)
    {
        using var response = await _httpClient.GetAsync(
            CombineUrl($"/translations/{jobId}/download"), HttpCompletionOption.ResponseHeadersRead, ct);
        await EnsureSuccessAsync(response, ct);
        await using var source = await response.Content.ReadAsStreamAsync(ct);
        await using var destination = File.Create(destinationPath);
        await source.CopyToAsync(destination, ct);
    }

    private string CombineUrl(string path) => BaseUrl.TrimEnd('/') + path;

    private static async Task EnsureSuccessAsync(HttpResponseMessage response, CancellationToken ct)
    {
        if (response.IsSuccessStatusCode)
        {
            return;
        }

        string detail = await response.Content.ReadAsStringAsync(ct);
        throw new InvalidOperationException($"Falha na API ({(int)response.StatusCode}): {detail}");
    }

    private static async Task<T> ReadJsonAsync<T>(HttpResponseMessage response, CancellationToken ct)
    {
        await using var stream = await response.Content.ReadAsStreamAsync(ct);
        var result = await JsonSerializer.DeserializeAsync<T>(stream, JsonOptions, ct);
        return result ?? throw new InvalidOperationException("Resposta vazia da API.");
    }
}
