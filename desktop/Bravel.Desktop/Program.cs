using Avalonia;
using Avalonia.Headless;
using Avalonia.Threading;
using Avalonia.Media.Imaging;
using System.Text.Json;

namespace Bravel.Desktop;

internal static class Program
{
    public static bool Preview { get; private set; }

    [STAThread]
    public static void Main(string[] args)
    {
        if (args.Length == 1 && args[0] == "--check-backend")
        {
            CheckBackend().GetAwaiter().GetResult();
            return;
        }
        if (args.Length == 2 && args[0] == "--render-preview")
        {
            Preview = true;
            AppBuilder.Configure<App>().UseSkia()
                .UseHeadless(new AvaloniaHeadlessPlatformOptions { UseHeadlessDrawing = false })
                .SetupWithoutStarting();
            var window = new MainWindow();
            window.Show();
            Dispatcher.UIThread.RunJobs();
            using var frame = window.CaptureRenderedFrame() ?? throw new InvalidOperationException("Frame unavailable");
            frame.Save(args[1], PngBitmapEncoderOptions.Default);
            window.Close();
            return;
        }
        AppBuilder.Configure<App>().UsePlatformDetect().LogToTrace().StartWithClassicDesktopLifetime(args);
    }

    private static async Task CheckBackend()
    {
        if (Environment.GetEnvironmentVariable("BRAVEL_TEST_MODE") != "1")
            throw new InvalidOperationException("Test mode required");
        using var client = new AgentClient();
        var status = await client.Call("status");
        if (!new Uri(status.GetProperty("base_url").GetString()!).IsLoopback)
            throw new InvalidOperationException("Only a local test fixture is allowed");
        var plan = await client.Call("plan", new { prompt = "desktop test fixture" });
        var identifier = plan.GetProperty("plan_id").GetString();
        var rejected = false;
        try { await client.Call("execute", new { plan_id = identifier, approval = "" }); }
        catch (InvalidOperationException) { rejected = true; }
        if (!rejected) throw new InvalidOperationException("Unapproved execution accepted");
        var result = await client.Call("execute", new { plan_id = identifier, approval = "approve" });
        if (!result.GetProperty("results")[0].GetProperty("output").GetString()!.Contains("desktop-observed"))
            throw new InvalidOperationException("Command output missing");
        var followup = await client.Call("plan", new { continuation = true });
        if (followup.GetProperty("steps").GetArrayLength() != 0)
            throw new InvalidOperationException("Followup failed");
        await client.Call("reset");
        Console.WriteLine("Desktop bridge smoke passed");
    }
}
