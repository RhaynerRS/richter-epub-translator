using System;
using System.Threading.Tasks;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Navigation;
using Lexicast.UI.Models;
using Windows.Storage.Pickers;
using WinRT.Interop;

namespace Lexicast.UI.Pages
{
    /// <summary>
    /// Second screen: polls job status until it finishes and lets the user download the result.
    /// </summary>
    public sealed partial class ProgressPage : Page
    {
        private readonly DispatcherTimer _pollTimer;
        private string? _jobId;
        private string _sourceFileName = string.Empty;
        private bool _isPolling;

        public ProgressPage()
        {
            InitializeComponent();
            _pollTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1.5) };
            _pollTimer.Tick += PollTimer_Tick;
        }

        protected override void OnNavigatedTo(NavigationEventArgs e)
        {
            base.OnNavigatedTo(e);
            if (e.Parameter is ProgressPageParameter parameter)
            {
                _jobId = parameter.JobId;
                _sourceFileName = parameter.SourceFileName;
                FileNameText.Text = _sourceFileName;
                _pollTimer.Start();
            }
        }

        protected override void OnNavigatedFrom(NavigationEventArgs e)
        {
            base.OnNavigatedFrom(e);
            _pollTimer.Stop();
        }

        private async void PollTimer_Tick(object? sender, object e)
        {
            if (_isPolling || _jobId is null)
            {
                return;
            }

            _isPolling = true;
            try
            {
                TranslationJob job = await App.ApiClient.GetJobAsync(_jobId);
                ApplyJobState(job);

                if (job.IsCompleted || job.IsFailed)
                {
                    _pollTimer.Stop();
                }
            }
            catch (Exception ex)
            {
                _pollTimer.Stop();
                ErrorInfoBar.Message = ex.Message;
                ErrorInfoBar.IsOpen = true;
            }
            finally
            {
                _isPolling = false;
            }
        }

        private void ApplyJobState(TranslationJob job)
        {
            double percent = Math.Clamp(job.Progress * 100.0, 0, 100);
            JobProgressBar.Value = percent;
            ProgressPercentText.Text = $"{percent:0}%";
            StatusText.Text = $"Status: {job.Status}";

            if (!string.IsNullOrEmpty(job.Warning))
            {
                WarningInfoBar.Message = job.Warning;
                WarningInfoBar.IsOpen = true;
            }

            if (job.IsFailed && !string.IsNullOrEmpty(job.Error))
            {
                ErrorInfoBar.Message = job.Error;
                ErrorInfoBar.IsOpen = true;
            }

            DownloadButton.IsEnabled = job.IsCompleted;
        }

        private async void DownloadButton_Click(object sender, RoutedEventArgs e)
        {
            if (_jobId is null)
            {
                return;
            }

            var savePicker = new FileSavePicker();
            savePicker.FileTypeChoices.Add("EPUB", new System.Collections.Generic.List<string> { ".epub" });
            savePicker.SuggestedFileName = $"translated_{_sourceFileName}";

            var hwnd = WindowNative.GetWindowHandle(App.MainWindowInstance);
            InitializeWithWindow.Initialize(savePicker, hwnd);

            var destinationFile = await savePicker.PickSaveFileAsync();
            if (destinationFile is null)
            {
                return;
            }

            DownloadButton.IsEnabled = false;
            DownloadProgressRing.IsActive = true;
            try
            {
                await App.ApiClient.DownloadAsync(_jobId, destinationFile.Path);
            }
            catch (Exception ex)
            {
                ErrorInfoBar.Message = ex.Message;
                ErrorInfoBar.IsOpen = true;
            }
            finally
            {
                DownloadButton.IsEnabled = true;
                DownloadProgressRing.IsActive = false;
            }
        }

        private void NewTranslationButton_Click(object sender, RoutedEventArgs e)
        {
            Frame.Navigate(typeof(UploadPage));
        }
    }
}
