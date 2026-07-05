using System;
using System.IO;
using System.Threading.Tasks;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Windows.Storage.Pickers;
using WinRT.Interop;

namespace Lexicast.UI.Pages
{
    /// <summary>
    /// First screen: pick an .epub file and configure the translation request.
    /// </summary>
    public sealed partial class UploadPage : Page
    {
        private string? _selectedFilePath;

        public UploadPage()
        {
            InitializeComponent();
        }

        private async void PickFileButton_Click(object sender, RoutedEventArgs e)
        {
            var picker = new FileOpenPicker();
            picker.FileTypeFilter.Add(".epub");

            var hwnd = WindowNative.GetWindowHandle(App.MainWindowInstance);
            InitializeWithWindow.Initialize(picker, hwnd);

            var file = await picker.PickSingleFileAsync();
            if (file is null)
            {
                return;
            }

            _selectedFilePath = file.Path;
            SelectedFileText.Text = file.Name;
        }

        private async void TranslateButton_Click(object sender, RoutedEventArgs e)
        {
            ErrorInfoBar.IsOpen = false;

            if (string.IsNullOrEmpty(_selectedFilePath))
            {
                ShowError("Selecione um arquivo .epub antes de continuar.");
                return;
            }

            string targetLanguage = TargetLanguageBox.Text.Trim();
            if (string.IsNullOrEmpty(targetLanguage))
            {
                ShowError("Informe o idioma de destino.");
                return;
            }

            string apiUrl = ApiUrlBox.Text.Trim();
            if (string.IsNullOrEmpty(apiUrl))
            {
                ShowError("Informe a URL da API.");
                return;
            }

            int concurrency = (int)ConcurrencyBox.Value;
            string? userPrompt = string.IsNullOrWhiteSpace(UserPromptBox.Text) ? null : UserPromptBox.Text.Trim();

            TranslateButton.IsEnabled = false;
            SubmitProgressRing.IsActive = true;

            try
            {
                App.ApiClient.BaseUrl = apiUrl;
                var job = await App.ApiClient.CreateTranslationAsync(
                    _selectedFilePath, targetLanguage, concurrency, userPrompt);

                Frame.Navigate(typeof(ProgressPage), new ProgressPageParameter(job.JobId, Path.GetFileName(_selectedFilePath)));
            }
            catch (Exception ex)
            {
                ShowError(ex.Message);
            }
            finally
            {
                TranslateButton.IsEnabled = true;
                SubmitProgressRing.IsActive = false;
            }
        }

        private void ShowError(string message)
        {
            ErrorInfoBar.Message = message;
            ErrorInfoBar.IsOpen = true;
        }
    }
}
