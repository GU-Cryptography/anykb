declare module "html2pdf.js" {
  type Html2PdfOptions = {
    margin?: number | [number, number, number, number];
    filename?: string;
    image?: { type?: string; quality?: number };
    html2canvas?: Record<string, unknown>;
    jsPDF?: Record<string, unknown>;
  };
  interface Html2PdfChain {
    set(opts: Html2PdfOptions): Html2PdfChain;
    from(el: HTMLElement): Html2PdfChain;
    save(): Promise<void>;
    outputPdf(type?: string): Promise<unknown>;
  }
  function html2pdf(): Html2PdfChain;
  export default html2pdf;
}
