using Avalonia;
using Avalonia.Controls;
using Avalonia.Input;
using Avalonia.Interactivity;
using Avalonia.Media;
using Avalonia.Threading;
using Avalonia.VisualTree;
using System.Text.Json;

namespace Bravel.Desktop;

public partial class MainWindow : Window
{
    private AgentClient? agent;
    private JsonElement? currentPlan;
    private JsonElement? currentStatus;
    private bool busy;
    private bool fillingSettings;
    private static readonly string[] Providers = ["openai", "gemini", "openrouter", "compatible"];

    public MainWindow()
    {
        InitializeComponent();
        Closed += (_, _) => agent?.Dispose();
        if (Program.Preview)
        {
            ProviderLabel.Text = "Gemini · gemini-3.5-flash-lite";
            StatusLabel.Text = "Готов к работе   ·   Enter — отправить, Shift+Enter — новая строка";
        }
        else Opened += async (_, _) => await Connect();
    }

    private async Task Connect()
    {
        SetBusy(true, "Подключение к агенту…");
        try
        {
            agent = new AgentClient();
            ApplyStatus(await agent.Call("status"));
            if (!currentStatus!.Value.GetProperty("key_set").GetBoolean()) ShowSettings();
        }
        catch (Exception error) { AddMessage("Не удалось подключиться", error.Message, "#FFB7BD"); }
        finally { SetBusy(false); }
    }

    private void ApplyStatus(JsonElement status)
    {
        currentStatus = status;
        ProviderLabel.Text = $"{status.GetProperty("provider").GetString()} · {status.GetProperty("model").GetString()}";
        ConfigLabel.Text = "Конфиг: " + status.GetProperty("config_path").GetString();
        StatusLabel.Text = "Готов к работе · " + status.GetProperty("cwd").GetString();
    }

    private void SetBusy(bool value, string? text = null)
    {
        busy = value;
        SendButton.IsEnabled = PromptInput.IsEnabled = NewButton.IsEnabled = SettingsButton.IsEnabled = !value;
        CancelButton.IsVisible = value && agent is not null;
        ContinueButton.IsEnabled = ApproveButton.IsEnabled = !value;
        if (text is not null) StatusLabel.Text = text;
        else if (!value) StatusLabel.Text = "Готов · Enter — отправить, Shift+Enter — новая строка";
    }

    private void AddMessage(string title, string body, string? accent = null, bool code = false)
    {
        Welcome.IsVisible = false;
        HistoryScroll.IsVisible = true;
        var stack = new StackPanel { Spacing = 8 };
        stack.Children.Add(new TextBlock
        {
            Text = title, FontSize = 11, FontWeight = FontWeight.SemiBold,
            Foreground = Brush.Parse(accent ?? "#AEBBFC")
        });
        stack.Children.Add(new SelectableTextBlock
        {
            Text = body, TextWrapping = TextWrapping.Wrap, FontSize = code ? 12 : 14,
            FontFamily = code ? new FontFamily("Cascadia Code,Consolas,DejaVu Sans Mono,monospace") : FontFamily.Default,
            Foreground = Brush.Parse("#EDEFFB")
        });
        Messages.Children.Add(new Border
        {
            Background = Brush.Parse(code ? "#500A1020" : "#12FFFFFF"),
            BorderBrush = Brush.Parse("#22FFFFFF"), BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(20), Padding = new Thickness(18, 15), Child = stack
        });
        Dispatcher.UIThread.Post(() => HistoryScroll.ScrollToEnd());
    }

    private async Task Plan(string prompt, bool continuation = false)
    {
        if (busy || agent is null) return;
        ApprovalPanel.IsVisible = ContinueButton.IsVisible = false;
        currentPlan = null;
        SetBusy(true, continuation ? "Анализирую результат…" : "Думаю над задачей…");
        try
        {
            var result = await agent.Call("plan", new { prompt, continuation });
            currentPlan = result;
            AddMessage("BRAVEL", result.GetProperty("summary").GetString() ?? "");
            var steps = result.GetProperty("steps");
            var index = 0;
            foreach (var step in steps.EnumerateArray())
            {
                index++;
                AddMessage($"ДЕЙСТВИЕ {index} · {(step.GetProperty("dangerous").GetBoolean() ? "ПОВЫШЕННЫЙ РИСК" : step.GetProperty("risk").GetString()?.ToUpperInvariant())}",
                    step.GetProperty("command").GetString() + "\n\n" + step.GetProperty("explanation").GetString(),
                    step.GetProperty("dangerous").GetBoolean() ? "#FFD8A8" : null, code: true);
            }
            ApprovalPanel.IsVisible = steps.GetArrayLength() > 0;
            RiskInput.IsVisible = result.GetProperty("dangerous").GetBoolean();
            RiskInput.Text = "";
            ApprovalLabel.Text = $"Выполнить {steps.GetArrayLength()} действие(й) в {result.GetProperty("cwd").GetString()}?";
        }
        catch (Exception error) { AddMessage("ЗАПРОС НЕ ВЫПОЛНЕН", error.Message, "#FFB7BD"); }
        finally { SetBusy(false); }
    }

    private async void SendPrompt(object? sender, RoutedEventArgs args)
    {
        var text = PromptInput.Text?.Trim();
        if (busy || string.IsNullOrWhiteSpace(text)) return;
        AddMessage("ТЫ", text, "#D0D8E9");
        PromptInput.Text = "";
        await Plan(text);
    }

    private void PromptKeyDown(object? sender, KeyEventArgs args)
    {
        if (args.Key == Key.Enter && !args.KeyModifiers.HasFlag(KeyModifiers.Shift))
        {
            args.Handled = true;
            SendPrompt(sender, new RoutedEventArgs());
        }
    }

    private void UseSuggestion(object? sender, RoutedEventArgs args)
    {
        if (sender is Button button) PromptInput.Text = button.Content?.ToString();
        PromptInput.Focus();
    }

    private async void Approve(object? sender, RoutedEventArgs args)
    {
        if (busy || agent is null || currentPlan is not { } plan) return;
        var high = plan.GetProperty("dangerous").GetBoolean();
        if (high && RiskInput.Text != "RUN")
        {
            ApprovalLabel.Text = "Для этого плана введите RUN точно как написано.";
            return;
        }
        currentPlan = null;
        ApprovalPanel.IsVisible = false;
        SetBusy(true, "Выполняю подтверждённые действия…");
        try
        {
            var result = await agent.Call("execute", new { plan_id = plan.GetProperty("plan_id").GetString(), approval = high ? "RUN" : "approve" });
            foreach (var item in result.GetProperty("results").EnumerateArray())
            {
                var output = item.GetProperty("output").GetString();
                var details = string.IsNullOrEmpty(output) ? "Команда завершилась без вывода." : output;
                if (item.GetProperty("truncated").GetBoolean()) details += "\n[Показана последняя часть вывода]";
                if (item.GetProperty("timed_out").GetBoolean()) details += "\n[Остановлено: лимит времени или объёма вывода]";
                if (item.GetProperty("cancelled").GetBoolean()) details += "\n[Остановлено пользователем]";
                AddMessage("РЕЗУЛЬТАТ · код " + item.GetProperty("exit_code"), details, code: true);
            }
            ContinueButton.IsVisible = !result.GetProperty("cancelled").GetBoolean();
            ApplyStatus(await agent.Call("status"));
        }
        catch (Exception error) { AddMessage("ВЫПОЛНЕНИЕ ОСТАНОВЛЕНО", error.Message, "#FFB7BD"); }
        finally { SetBusy(false); }
    }

    private async void ContinueAgent(object? sender, RoutedEventArgs args) => await Plan("", continuation: true);
    private async void Decline(object? sender, RoutedEventArgs args)
    {
        currentPlan = null;
        ApprovalPanel.IsVisible = false;
        AddMessage("ОТМЕНЕНО", "Команды не выполнялись.");
        if (agent is not null) try { await agent.Call("cancel"); } catch (Exception) { }
    }

    private async void Cancel(object? sender, RoutedEventArgs args)
    {
        if (agent is null) return;
        StatusLabel.Text = "Останавливаю…";
        try { await agent.Call("cancel"); } catch (Exception) { }
    }

    private async void NewChat(object? sender, RoutedEventArgs args)
    {
        if (busy || agent is null) return;
        SetBusy(true);
        try
        {
            ApplyStatus(await agent.Call("reset"));
            Messages.Children.Clear();
            Welcome.IsVisible = true;
            HistoryScroll.IsVisible = ApprovalPanel.IsVisible = ContinueButton.IsVisible = false;
            currentPlan = null;
        }
        catch (Exception error) { AddMessage("ОШИБКА", error.Message, "#FFB7BD"); }
        finally { SetBusy(false); }
    }

    private void ShowSettings()
    {
        if (currentStatus is not { } status) return;
        fillingSettings = true;
        ProviderInput.SelectedIndex = Array.IndexOf(Providers, status.GetProperty("provider").GetString());
        UrlInput.Text = status.GetProperty("base_url").GetString();
        ModelInput.Text = status.GetProperty("model").GetString();
        KeyInput.Text = SettingsError.Text = "";
        fillingSettings = false;
        SettingsPanel.IsVisible = true;
    }
    private void OpenSettings(object? sender, RoutedEventArgs args) => ShowSettings();
    private void CloseSettings(object? sender, RoutedEventArgs args) { SettingsPanel.IsVisible = false; KeyInput.Text = ""; }
    private void ProviderChanged(object? sender, SelectionChangedEventArgs args)
    {
        if (fillingSettings || currentStatus is not { } status || ProviderInput.SelectedIndex < 0) return;
        var defaults = status.GetProperty("providers").GetProperty(Providers[ProviderInput.SelectedIndex]);
        UrlInput.Text = defaults.GetProperty("base_url").GetString();
        ModelInput.Text = defaults.GetProperty("model").GetString();
        KeyInput.Text = "";
    }
    private async void SaveSettings(object? sender, RoutedEventArgs args)
    {
        if (agent is null || ProviderInput.SelectedIndex < 0 || busy) return;
        SaveButton.IsEnabled = false;
        try
        {
            ApplyStatus(await agent.Call("configure", new
            {
                provider = Providers[ProviderInput.SelectedIndex], base_url = UrlInput.Text?.Trim(),
                model = ModelInput.Text?.Trim(), api_key = KeyInput.Text?.Trim() ?? ""
            }));
            KeyInput.Text = "";
            SettingsPanel.IsVisible = false;
        }
        catch (Exception error) { SettingsError.Text = error.Message; }
        finally { SaveButton.IsEnabled = true; }
    }
    private void DragWindow(object? sender, PointerPressedEventArgs args)
    {
        if (args.Source is Visual source && (source is Button || source.GetVisualAncestors().OfType<Button>().Any()))
            return;
        if (args.Source is TextBlock or StackPanel or Grid && args.GetCurrentPoint(this).Properties.IsLeftButtonPressed)
            BeginMoveDrag(args);
    }
    private void Minimize(object? sender, RoutedEventArgs args) => WindowState = WindowState.Minimized;
    private void CloseWindow(object? sender, RoutedEventArgs args) => Close();
}
