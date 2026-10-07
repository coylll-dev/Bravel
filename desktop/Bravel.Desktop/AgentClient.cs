using System.Collections.Concurrent;
using System.Diagnostics;
using System.Text;
using System.Text.Json;

namespace Bravel.Desktop;

internal sealed class AgentClient : IDisposable
{
    private readonly Process process;
    private readonly ConcurrentDictionary<int, TaskCompletionSource<JsonElement>> pending = new();
    private readonly SemaphoreSlim writer = new(1, 1);
    private int nextId;
    private bool disposed;

    public AgentClient()
    {
        var start = new ProcessStartInfo(FindPython())
        {
            UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardInput = true, RedirectStandardOutput = true, RedirectStandardError = true,
            StandardInputEncoding = new UTF8Encoding(false), StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8
        };
        start.ArgumentList.Add("-u"); start.ArgumentList.Add("-m"); start.ArgumentList.Add("bravel.bridge");
        start.Environment["PYTHONUTF8"] = "1";
        process = Process.Start(start) ?? throw new InvalidOperationException("Не удалось запустить Bravel.");
        _ = DrainErrors();
        _ = ReadResponses();
    }

    private static string FindPython()
    {
        var explicitPath = Environment.GetEnvironmentVariable("BRAVEL_PYTHON");
        if (!string.IsNullOrWhiteSpace(explicitPath)) return explicitPath;
        var home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        var root = OperatingSystem.IsWindows()
            ? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Bravel")
            : Path.Combine(Environment.GetEnvironmentVariable("XDG_DATA_HOME") ?? Path.Combine(home, ".local", "share"), "bravel");
        var relative = OperatingSystem.IsWindows() ? "venv/Scripts/python.exe" : "venv/bin/python";
        var installed = Path.Combine(root, relative);
        if (File.Exists(installed)) return installed;
        for (var directory = new DirectoryInfo(AppContext.BaseDirectory); directory is not null; directory = directory.Parent)
        {
            if (!File.Exists(Path.Combine(directory.FullName, "pyproject.toml"))) continue;
            var local = Path.Combine(directory.FullName, OperatingSystem.IsWindows() ? ".venv/Scripts/python.exe" : ".venv/bin/python");
            if (File.Exists(local)) return local;
        }
        throw new InvalidOperationException("Установите Bravel: python install.py. Или задайте BRAVEL_PYTHON — путь к Python с установленным Bravel.");
    }

    public async Task<JsonElement> Call(string method, object? parameters = null)
    {
        ObjectDisposedException.ThrowIf(disposed, this);
        var id = Interlocked.Increment(ref nextId);
        var completion = new TaskCompletionSource<JsonElement>(TaskCreationOptions.RunContinuationsAsynchronously);
        pending[id] = completion;
        try
        {
            var line = JsonSerializer.Serialize(new { id, method, @params = parameters ?? new { } });
            await writer.WaitAsync();
            try { await process.StandardInput.WriteLineAsync(line); await process.StandardInput.FlushAsync(); }
            finally { writer.Release(); }
            return await completion.Task.WaitAsync(TimeSpan.FromMinutes(3));
        }
        finally { pending.TryRemove(id, out _); }
    }

    private async Task DrainErrors()
    {
        // Never render raw backend stderr, which may contain environment information.
        var buffer = new char[2048];
        try { while (await process.StandardError.ReadAsync(buffer) != 0) { } }
        catch (Exception) { }
    }

    private async Task ReadResponses()
    {
        try
        {
            while (await process.StandardOutput.ReadLineAsync() is { } line)
            {
                using var document = JsonDocument.Parse(line);
                var message = document.RootElement;
                if (!message.GetProperty("id").TryGetInt32(out var id) || !pending.TryGetValue(id, out var completion)) continue;
                var error = message.GetProperty("error");
                if (error.ValueKind == JsonValueKind.String) completion.TrySetException(new InvalidOperationException(error.GetString()));
                else completion.TrySetResult(message.GetProperty("result").Clone());
            }
        }
        catch (Exception) { }
        finally
        {
            foreach (var request in pending.Values)
                request.TrySetException(new InvalidOperationException("Связь с агентом прервана. Обновите Bravel и откройте окно заново."));
        }
    }

    public void Dispose()
    {
        if (disposed) return;
        disposed = true;
        try { process.StandardInput.Close(); } catch (Exception) { }
        // EOF cancels the current command. Allow the backend time to stop its process group.
        _ = Task.Run(async () =>
        {
            try
            {
                await process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(4));
            }
            catch (Exception) { try { if (!process.HasExited) process.Kill(true); } catch (Exception) { } }
            finally { process.Dispose(); }
        });
    }
}
