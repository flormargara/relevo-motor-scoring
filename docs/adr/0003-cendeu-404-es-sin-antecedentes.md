# Un 404 de la Central de Deudores significa "sin antecedentes", no un error

La API del BCRA responde 404 cuando no hay deuda informada para la identificación. Ese 404 se modela como el resultado de dominio *sin antecedentes*, que la política trata como un dato más (por ejemplo, derivar a la PyME sin historial). Solo un timeout o un 5xx, después de un reintento, se tratan como *fuente no disponible*. No hay que "arreglar" este manejo para que el 404 se convierta en una excepción.
